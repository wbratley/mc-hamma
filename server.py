import asyncio
import argparse
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from meshcore import MeshCore, EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

HISTORY_FILE = Path("chat_history.json")
DM_HISTORY_DIR = Path("dm_history")
CONTACT_STATS_FILE = Path("contact_stats.json")
SMART_ADD_FILE = Path("smart_add_config.json")
HISTORY_MAX  = 1000

connected_clients: Set[WebSocket] = set()
message_buffer: deque = deque(maxlen=HISTORY_MAX)
dm_buffers: dict = {}           # pubkey_prefix -> deque(maxlen=1000) of DM messages
msg_id_counter = count(1)
channel_names: dict = {}    # channel_idx -> name
contacts: dict = {}         # full pubkey (hex) -> {name, lat, lon, ...}
contact_stats: dict = {}    # sender name (lower) -> {msg_count, last_chat}
pending_contacts: dict = {} # pubkey -> full contact dict (seen but not yet added)
autoadd_config: int = -1    # -1 = unknown; 0 = off; 1+ = on
smart_add_config: dict = {"nodes": False, "paths": False, "chatters": False}
neighbors: dict = {}        # last-hop hash (hex) -> neighbor entry
relay_windows: dict = {}    # channel_idx -> relay tracking entry
_rx_scratch: dict = {}      # RX_LOG_DATA → CHANNEL_MSG_RECV correlation
mc_instance = None

RELAY_WINDOW_SECS = 20      # how long to listen for echoes after sending

# Set at startup from CLI args
serial_port: str = ""
serial_baud: int = 115200


def dm_file(key: str) -> Path:
    DM_HISTORY_DIR.mkdir(exist_ok=True)
    return DM_HISTORY_DIR / f"{key}.json"


def load_dm_history(key: str) -> deque:
    buf: deque = deque(maxlen=HISTORY_MAX)
    f = dm_file(key)
    if f.exists():
        try:
            msgs = json.loads(f.read_text())
            if isinstance(msgs, list):
                for m in msgs[-HISTORY_MAX:]:
                    buf.append(m)
        except Exception as exc:
            logger.warning(f"Could not load DM history for {key}: {exc}")
    return buf


def save_dm_history(key: str) -> None:
    try:
        dm_file(key).write_text(json.dumps(list(dm_buffers[key])))
    except Exception as exc:
        logger.warning(f"Could not save DM history for {key}: {exc}")


def load_history() -> None:
    """Populate message_buffer from disk and advance msg_id_counter past stored ids."""
    global msg_id_counter
    if not HISTORY_FILE.exists():
        return
    try:
        msgs = json.loads(HISTORY_FILE.read_text())
        if not isinstance(msgs, list):
            return
        for m in msgs[-HISTORY_MAX:]:
            message_buffer.append(m)
        if message_buffer:
            max_id = max(m.get("id", 0) for m in message_buffer)
            msg_id_counter = count(max_id + 1)
        logger.info(f"Loaded {len(message_buffer)} messages from {HISTORY_FILE}")
    except Exception as exc:
        logger.warning(f"Could not load history: {exc}")


def save_history() -> None:
    """Write message_buffer to disk."""
    try:
        HISTORY_FILE.write_text(json.dumps(list(message_buffer)))
    except Exception as exc:
        logger.warning(f"Could not save history: {exc}")


async def broadcast(message: dict) -> None:
    if not connected_clients:
        return
    data = json.dumps(message)
    dead: Set[WebSocket] = set()
    for ws in list(connected_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


def find_contact(hop_hash: str) -> dict:
    """Return the contact whose pubkey starts with hop_hash, or {}."""
    for pubkey, c in contacts.items():
        if pubkey.startswith(hop_hash):
            return c
    return {}


def find_pubkey_by_name(name: str) -> str | None:
    """Return the pubkey for a contact with the given name, or None."""
    nl = name.lower()
    for pubkey, c in contacts.items():
        if (c.get("name") or "").lower() == nl:
            return pubkey
    return None


def load_contact_stats() -> None:
    if not CONTACT_STATS_FILE.exists():
        return
    try:
        data = json.loads(CONTACT_STATS_FILE.read_text())
        if isinstance(data, dict):
            contact_stats.update(data)
        logger.info(f"Loaded stats for {len(contact_stats)} contacts")
    except Exception as exc:
        logger.warning(f"Could not load contact stats: {exc}")


def save_contact_stats() -> None:
    try:
        CONTACT_STATS_FILE.write_text(json.dumps(contact_stats))
    except Exception as exc:
        logger.warning(f"Could not save contact stats: {exc}")


def load_smart_add_config() -> None:
    if not SMART_ADD_FILE.exists():
        return
    try:
        data = json.loads(SMART_ADD_FILE.read_text())
        for k in ("nodes", "paths", "chatters"):
            if k in data:
                smart_add_config[k] = bool(data[k])
        logger.info(f"Loaded smart_add_config: {smart_add_config}")
    except Exception as exc:
        logger.warning(f"Could not load smart_add_config: {exc}")


def save_smart_add_config() -> None:
    try:
        SMART_ADD_FILE.write_text(json.dumps(smart_add_config))
    except Exception as exc:
        logger.warning(f"Could not save smart_add_config: {exc}")


NODE_TYPE_NAMES = {0: "Client", 1: "Repeater", 2: "Room Server", 3: "Gateway"}


def serialize_contact(pubkey: str, c: dict) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    raw_advert = c.get("last_advert")
    # Cap future timestamps — device clocks can be wrong
    last_advert = min(raw_advert, now) if raw_advert else None

    name = c.get("name") or pubkey[:12]
    stats = contact_stats.get(name.lower(), {})

    return {
        "type":           "contact_update",
        "key":            pubkey[:12],
        "name":           name,
        "node_type":      c.get("node_type"),
        "node_type_name": NODE_TYPE_NAMES.get(c.get("node_type"), "Unknown"),
        "out_path_len":   c.get("out_path_len"),
        "last_advert":    last_advert,
        "last_chat":      stats.get("last_chat"),
        "chat_count":     stats.get("msg_count", 0),
        "lat":            c.get("lat"),
        "lon":            c.get("lon"),
    }


def serialize_pending(c: dict) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    raw = c.get("last_advert")
    return {
        "pubkey":      c["public_key"],
        "name":        c.get("adv_name") or c["public_key"][:12],
        "node_type":   c.get("type"),
        "node_type_name": NODE_TYPE_NAMES.get(c.get("type"), "Unknown"),
        "out_path_len": c.get("out_path_len"),
        "lat":         c.get("adv_lat"),
        "lon":         c.get("adv_lon"),
        "last_advert": min(raw, now) if raw else None,
    }


def build_contacts_snapshot() -> dict:
    return {
        "type":     "contacts_snapshot",
        "contacts": [serialize_contact(k, v) for k, v in contacts.items()],
    }


BUCKET_SECS  = 5 * 60          # 5-minute buckets
HISTORY_SECS = 2 * 60 * 60    # 2 hours of history
N_BUCKETS    = HISTORY_SECS // BUCKET_SECS   # 24 buckets


def serialize_neighbor(n: dict) -> dict:
    snr_hist  = list(n["snr_history"])
    rssi_hist = list(n["rssi_history"])
    contact   = find_contact(n["key"])

    # Build 24 time-series buckets (oldest → newest)
    now    = int(datetime.now(timezone.utc).timestamp())
    start  = now - HISTORY_SECS
    counts = [0] * N_BUCKETS
    for ts in n.get("msg_timestamps", []):
        if ts >= start:
            idx = min(int((ts - start) // BUCKET_SECS), N_BUCKETS - 1)
            counts[idx] += 1

    return {
        "type":         "neighbor_update",
        "key":          n["key"],
        "name":         contact.get("name") or n["key"],
        "lat":          contact.get("lat"),
        "lon":          contact.get("lon"),
        "avg_snr":      round(sum(snr_hist)  / len(snr_hist),  1) if snr_hist  else None,
        "avg_rssi":     round(sum(rssi_hist) / len(rssi_hist), 1) if rssi_hist else None,
        "msg_count":    n.get("msg_count", 0),
        "time_series":  counts,
        "last_seen":    n["last_seen"],
    }


async def meshcore_listener() -> None:
    global mc_instance
    logger.info(f"Connecting to MeshCore on {serial_port} at {serial_baud} baud")
    while True:
        try:
            mc = await MeshCore.create_serial(serial_port, serial_baud)
            mc_instance = mc
            logger.info(f"Connected to MeshCore on {serial_port}")

            async def on_any_event(event) -> None:
                logger.info(f"EVENT [{event.type.name}] payload={event.payload!r} attrs={event.attributes!r}")

            async def on_channel_info(event) -> None:
                payload = event.payload
                idx  = payload.get("channel_idx")
                name = payload.get("channel_name")
                if idx is not None and name:
                    channel_names[idx] = name
                    logger.info(f"Channel {idx} name: {name!r}")

            async def on_channel_msg(event) -> None:
                payload     = event.payload
                channel_idx = payload.get("channel_idx", 0)
                ts = payload.get(
                    "sender_timestamp",
                    int(datetime.now(timezone.utc).timestamp()),
                )
                raw_text = payload.get("text", "")
                if ": " in raw_text:
                    sender, text = raw_text.split(": ", 1)
                else:
                    sender = payload.get("pubkey_prefix", "Unknown")
                    text   = raw_text

                # Consume path info stashed by on_rx_log
                rx = _rx_scratch.pop("last_grp_txt", {})
                path_str       = rx.get("path", "")
                path_hash_size = rx.get("path_hash_size", 1)
                chars_per_hop  = path_hash_size * 2
                path_nodes: list = []
                if path_str:
                    for i in range(0, len(path_str), chars_per_hop):
                        h = path_str[i:i + chars_per_hop]
                        c = find_contact(h)
                        path_nodes.append({"hash": h, "name": c.get("name")})

                msg = {
                    "id": next(msg_id_counter),
                    "type": "message",
                    "text": text,
                    "sender": sender,
                    "timestamp": ts,
                    "channel_idx": channel_idx,
                    "channel_name": channel_names.get(channel_idx),
                    "hops": len(path_nodes),
                    "path": path_nodes,
                    "snr":  rx.get("snr"),
                    "rssi": rx.get("rssi"),
                    "sender_lat": None,
                    "sender_lon": None,
                }
                message_buffer.append(msg)
                save_history()

                # Update per-contact chat stats
                key = sender.lower()
                if key not in contact_stats:
                    contact_stats[key] = {"msg_count": 0, "last_chat": None}
                contact_stats[key]["msg_count"] += 1
                contact_stats[key]["last_chat"] = ts
                save_contact_stats()

                # Broadcast updated contact card if we know this contact;
                # also attach sender location to the message for the UI
                pubkey = find_pubkey_by_name(sender)
                if pubkey and pubkey in contacts:
                    c = contacts[pubkey]
                    msg["sender_lat"] = c.get("lat")
                    msg["sender_lon"] = c.get("lon")
                    await broadcast(serialize_contact(pubkey, c))

                logger.info(f"Channel {channel_idx} message from {sender!r}: {text!r}")
                await broadcast(msg)

            async def on_contact(event) -> None:
                """Store contacts from CONTACTS / NEW_CONTACT / NEXT_CONTACT events."""
                p = event.payload
                # Payload may be a list (CONTACTS) or a single dict
                entries = p if isinstance(p, list) else [p]
                for c in entries:
                    pubkey = c.get("public_key") or c.get("adv_key") or c.get("pubkey")
                    name   = c.get("adv_name") or c.get("name")
                    if pubkey and name:
                        contacts[pubkey] = {
                            "name":         name,
                            "lat":          c.get("adv_lat"),
                            "lon":          c.get("adv_lon"),
                            "node_type":    c.get("type"),
                            "out_path_len": c.get("out_path_len"),
                            "last_advert":  c.get("last_advert"),
                        }
                        logger.info(f"Contact: {name!r} key={pubkey[:12]}…")
                        await broadcast(serialize_contact(pubkey, contacts[pubkey]))
                # After a full contacts list, also send snapshot
                if isinstance(p, dict):
                    await broadcast(build_contacts_snapshot())

            async def on_rx_log(event) -> None:
                """Track last-hop neighbors and detect echoes of our sent messages."""
                payload        = event.payload
                path           = payload.get("path", "")
                path_hash_size = max(payload.get("path_hash_size", 1), 1)
                snr            = payload.get("snr")
                rssi           = payload.get("rssi")
                pkt_hash       = payload.get("pkt_hash")

                if not path or snr is None:
                    return

                # Stash path info for GRP_TXT so on_channel_msg can pick it up
                if payload.get("payload_type") == 5:
                    _rx_scratch["last_grp_txt"] = {
                        "path": path,
                        "path_hash_size": path_hash_size,
                        "snr":  snr,
                        "rssi": rssi,
                    }

                # Last entry in path = the node that directly handed us the packet
                chars_per_hop = path_hash_size * 2
                hop_hash      = path[-chars_per_hop:]

                # ── Relay detection ──────────────────────────────────────────
                # GRP_TXT (channel text) = payload_type 5
                if payload.get("payload_type") == 5 and pkt_hash is not None:
                    now = int(datetime.now(timezone.utc).timestamp())
                    for ch_idx, win in list(relay_windows.items()):
                        age = now - win["sent_at"]
                        if age > RELAY_WINDOW_SECS:
                            continue
                        if win["pkt_hash"] is None:
                            # First GRP_TXT after our send — assume it's our echo
                            win["pkt_hash"] = pkt_hash
                        if pkt_hash != win["pkt_hash"]:
                            continue  # different message, skip
                        if hop_hash not in win["relayers"]:
                            win["relayers"].add(hop_hash)
                            contact = find_contact(hop_hash)
                            logger.info(
                                f"Relay heard for msg {win['msg_id']}: "
                                f"{contact.get('name', hop_hash)} ({hop_hash})"
                            )
                            await broadcast({
                                "type":    "relay_update",
                                "msg_id":  win["msg_id"],
                                "relayers": [
                                    {"key": h, "name": find_contact(h).get("name") or h}
                                    for h in win["relayers"]
                                ],
                                "final": False,
                            })

                if hop_hash not in neighbors:
                    neighbors[hop_hash] = {
                        "key":            hop_hash,
                        "snr_history":    deque(maxlen=5),
                        "rssi_history":   deque(maxlen=5),
                        "last_seen":      0,
                        "msg_count":      0,
                        "msg_timestamps": deque(maxlen=1500),  # ~2h at 1 pkt/5s
                    }

                n = neighbors[hop_hash]
                n["snr_history"].append(snr)
                if rssi is not None:
                    n["rssi_history"].append(rssi)
                now_ts = int(datetime.now(timezone.utc).timestamp())
                n["last_seen"]   = now_ts
                n["msg_count"]  += 1
                n["msg_timestamps"].append(now_ts)

                contact = find_contact(hop_hash)
                logger.info(f"Last-hop {hop_hash!r} ({contact.get('name', '?')}) snr={snr} rssi={rssi}")
                await broadcast(serialize_neighbor(n))

            async def on_contact_msg(event) -> None:
                """Handle incoming direct (contact) messages."""
                payload    = event.payload
                key        = payload.get("pubkey_prefix", "unknown")
                text       = payload.get("text", "")
                ts = payload.get(
                    "sender_timestamp",
                    int(datetime.now(timezone.utc).timestamp()),
                )
                contact = find_contact(key)
                sender  = contact.get("name") or key

                if key not in dm_buffers:
                    dm_buffers[key] = load_dm_history(key)

                dm_msg = {
                    "id":           next(msg_id_counter),
                    "type":         "dm",
                    "contact_key":  key,
                    "contact_name": sender,
                    "text":         text,
                    "sender":       sender,
                    "timestamp":    ts,
                    "own":          False,
                }
                dm_buffers[key].append(dm_msg)
                save_dm_history(key)
                logger.info(f"DM from {sender!r} ({key}): {text!r}")
                await broadcast(dm_msg)

            mc.subscribe(None, on_any_event)
            mc.subscribe(EventType.CHANNEL_INFO,     on_channel_info)
            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_msg)
            mc.subscribe(EventType.CONTACT_MSG_RECV, on_contact_msg)
            mc.subscribe(EventType.RX_LOG_DATA,      on_rx_log)
            async def on_new_contact_pending(event) -> None:
                """NEW_CONTACT push = node heard (PUSH_CODE_NEW_ADVERT).
                If autoadd is off, the device won't save it — check smart_add filters
                first; if matched, add immediately; otherwise queue as pending.
                If autoadd is on, the device saved it — mirror locally."""
                global autoadd_config
                c = event.payload
                pubkey = c.get("public_key")
                if not pubkey:
                    return
                if autoadd_config == 0:
                    if pubkey not in contacts:
                        reasons = check_smart_add(c)
                        if reasons:
                            logger.info(
                                f"Smart-adding {c.get('adv_name')!r} {pubkey[:12]}… "
                                f"({', '.join(reasons)})"
                            )
                            await do_add_contact(pubkey, c)
                        else:
                            pending_contacts[pubkey] = c
                            logger.info(f"Pending contact: {c.get('adv_name')!r} {pubkey[:12]}…")
                            await broadcast({
                                "type":    "pending_contact",
                                "contact": serialize_pending(c),
                            })
                else:
                    # Autoadd on (or unknown) — device saved it; mirror locally
                    await on_contact(event)

            async def on_autoadd_config(event) -> None:
                global autoadd_config
                autoadd_config = event.payload.get("config", -1)
                logger.info(f"Autoadd config: {autoadd_config}")
                await broadcast({"type": "autoadd_config", "value": autoadd_config})

            mc.subscribe(EventType.CONTACTS,         on_contact)
            mc.subscribe(EventType.NEW_CONTACT,      on_new_contact_pending)
            mc.subscribe(EventType.NEXT_CONTACT,     on_contact)
            mc.subscribe(EventType.AUTOADD_CONFIG,   on_autoadd_config)

            await mc.start_auto_message_fetching()

            for idx in range(8):
                try:
                    await mc.commands.get_channel(idx)
                except Exception:
                    pass

            # Fetch contacts so we can resolve neighbor names
            try:
                await mc.commands.get_contacts()
            except Exception:
                pass

            # Fetch autoadd config
            try:
                await mc.commands.get_autoadd_config()
            except Exception:
                pass

            await asyncio.sleep(float("inf"))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"MeshCore error: {exc} — retrying in 5 s")
            mc_instance = None
            await asyncio.sleep(5)


async def handle_set_smart_add(packet: dict) -> None:
    for k in ("nodes", "paths", "chatters"):
        if k in packet:
            smart_add_config[k] = bool(packet[k])
    save_smart_add_config()
    logger.info(f"Smart-add config updated: {smart_add_config}")
    await broadcast({"type": "smart_add_config", "config": dict(smart_add_config)})


async def handle_set_autoadd(packet: dict) -> None:
    global autoadd_config
    value = int(packet.get("value", 0))
    if mc_instance is None:
        return
    try:
        await mc_instance.commands.set_autoadd_config(value)
        autoadd_config = value
        logger.info(f"Autoadd set to {value}")
        await broadcast({"type": "autoadd_config", "value": autoadd_config})
    except Exception as exc:
        logger.error(f"set_autoadd failed: {exc}")


def check_smart_add(c: dict) -> list:
    """Return list of match reasons if smart_add_config says this contact should be auto-added."""
    reasons = []
    pubkey    = (c.get("public_key") or "").lower()
    node_type = c.get("type")
    name      = (c.get("adv_name") or "").lower()

    if smart_add_config.get("nodes") and node_type == 1:
        for hop_hash in neighbors:
            if pubkey.startswith(hop_hash.lower()):
                reasons.append("repeater in nodes")
                break

    if smart_add_config.get("paths") and node_type == 1:
        found = False
        for msg in message_buffer:
            for node in msg.get("path", []):
                h = (node.get("hash") or "").lower()
                if h and pubkey.startswith(h):
                    reasons.append("repeater in paths")
                    found = True
                    break
            if found:
                break

    if smart_add_config.get("chatters") and name and name in contact_stats:
        reasons.append("seen chatting")

    return reasons


async def do_add_contact(pubkey: str, c: dict) -> None:
    """Add a contact to device memory and move from pending to contacts dict."""
    if mc_instance is None:
        return
    try:
        await mc_instance.commands.add_contact(c)
        name = c.get("adv_name") or pubkey[:12]
        contacts[pubkey] = {
            "name":         name,
            "lat":          c.get("adv_lat"),
            "lon":          c.get("adv_lon"),
            "node_type":    c.get("type"),
            "out_path_len": c.get("out_path_len"),
            "last_advert":  c.get("last_advert"),
        }
        pending_contacts.pop(pubkey, None)
        logger.info(f"Added contact {name!r} {pubkey[:12]}…")
        await broadcast(serialize_contact(pubkey, contacts[pubkey]))
        await broadcast({"type": "pending_contact_removed", "pubkey": pubkey})
    except Exception as exc:
        logger.error(f"add_contact failed: {exc}")


async def handle_add_contact(packet: dict) -> None:
    pubkey = packet.get("pubkey", "")
    if not pubkey or mc_instance is None:
        return
    c = pending_contacts.get(pubkey)
    if not c:
        logger.warning(f"add_contact: {pubkey[:12]} not in pending_contacts")
        return
    await do_add_contact(pubkey, c)


async def handle_remove_contact(packet: dict) -> None:
    pubkey = packet.get("pubkey", "")
    if not pubkey or mc_instance is None:
        return
    try:
        await mc_instance.commands.remove_contact(pubkey)
        contacts.pop(pubkey, None)
        logger.info(f"Removed contact {pubkey[:12]}…")
        await broadcast({"type": "contact_removed", "pubkey": pubkey[:12]})
        await broadcast(build_contacts_snapshot())
    except Exception as exc:
        logger.error(f"remove_contact failed: {exc}")


async def handle_purge_contacts() -> None:
    if mc_instance is None:
        return
    keys = list(contacts.keys())
    for pubkey in keys:
        try:
            await mc_instance.commands.remove_contact(pubkey)
        except Exception as exc:
            logger.error(f"purge remove {pubkey[:12]}: {exc}")
    contacts.clear()
    logger.info(f"Purged {len(keys)} contacts")
    await broadcast({"type": "contacts_cleared"})


async def handle_send_dm(packet: dict) -> None:
    text        = packet.get("text", "").strip()
    contact_key = packet.get("contact_key", "")
    if not text or not contact_key or mc_instance is None:
        return
    try:
        await mc_instance.commands.send_msg(contact_key, text)
        contact = find_contact(contact_key)
        contact_name = contact.get("name") or contact_key

        if contact_key not in dm_buffers:
            dm_buffers[contact_key] = load_dm_history(contact_key)

        dm_msg = {
            "id":           next(msg_id_counter),
            "type":         "dm",
            "contact_key":  contact_key,
            "contact_name": contact_name,
            "text":         text,
            "sender":       "You",
            "timestamp":    int(datetime.now(timezone.utc).timestamp()),
            "own":          True,
        }
        dm_buffers[contact_key].append(dm_msg)
        save_dm_history(contact_key)
        logger.info(f"DM sent to {contact_name!r} ({contact_key}): {text!r}")
        await broadcast(dm_msg)
    except Exception as exc:
        logger.error(f"DM send failed: {exc}")


async def handle_send(packet: dict) -> None:
    text        = packet.get("text", "").strip()
    channel_idx = packet.get("channel_idx", 0)
    if not text or mc_instance is None:
        return
    try:
        await mc_instance.commands.send_chan_msg(channel_idx, text)
        msg = {
            "id": next(msg_id_counter),
            "type": "message",
            "text": text,
            "sender": "You",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "channel_idx": channel_idx,
            "channel_name": channel_names.get(channel_idx),
            "own": True,
        }
        message_buffer.append(msg)
        save_history()
        logger.info(f"Sent on channel {channel_idx}: {text!r}")
        await broadcast(msg)

        # Open a relay-detection window for this channel
        relay_windows[channel_idx] = {
            "msg_id":   msg["id"],
            "sent_at":  msg["timestamp"],
            "pkt_hash": None,      # filled in when first echo arrives
            "relayers": set(),
        }
        asyncio.create_task(_relay_timeout(channel_idx, msg["id"]))
    except Exception as exc:
        logger.error(f"Send failed: {exc}")


async def _relay_timeout(channel_idx: int, msg_id: int) -> None:
    await asyncio.sleep(RELAY_WINDOW_SECS)
    win = relay_windows.pop(channel_idx, None)
    if win and win["msg_id"] == msg_id:
        relayers = [
            {"key": h, "name": find_contact(h).get("name") or h}
            for h in win["relayers"]
        ]
        logger.info(
            f"Relay window closed for msg {msg_id}: {len(relayers)} relayer(s)"
        )
        await broadcast({
            "type":     "relay_update",
            "msg_id":   msg_id,
            "relayers": relayers,
            "final":    True,
        })


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_history()
    load_contact_stats()
    load_smart_add_config()
    # Pre-load any saved per-contact DM history
    if DM_HISTORY_DIR.exists():
        for f in DM_HISTORY_DIR.glob("*.json"):
            key = f.stem
            dm_buffers[key] = load_dm_history(key)
            logger.info(f"Loaded {len(dm_buffers[key])} DMs for contact {key}")
    task = asyncio.create_task(meshcore_listener())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connected_clients.add(websocket)
    logger.info(f"WebSocket client connected — total: {len(connected_clients)}")
    try:
        # Connect confirmation
        test_msg = {
            "id": 0,
            "type": "message",
            "text": f"WebSocket connected to {serial_port}",
            "sender": "server",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "channel_idx": None,
            "channel_name": "system",
        }
        await websocket.send_text(json.dumps(test_msg))

        # Message history
        if message_buffer:
            await websocket.send_text(
                json.dumps({"type": "history", "messages": list(message_buffer)})
            )

        # DM history — send each contact's thread
        if dm_buffers:
            all_dms = []
            for buf in dm_buffers.values():
                all_dms.extend(buf)
            all_dms.sort(key=lambda m: m["timestamp"])
            await websocket.send_text(
                json.dumps({"type": "dm_history", "messages": all_dms})
            )

        # Contacts snapshot
        if contacts:
            await websocket.send_text(json.dumps(build_contacts_snapshot()))

        # Autoadd config
        await websocket.send_text(json.dumps({"type": "autoadd_config", "value": autoadd_config}))

        # Smart-add config
        await websocket.send_text(json.dumps({"type": "smart_add_config", "config": dict(smart_add_config)}))

        # Pending contacts
        if pending_contacts:
            await websocket.send_text(json.dumps({
                "type":     "pending_contacts_snapshot",
                "contacts": [serialize_pending(c) for c in pending_contacts.values()],
            }))

        # Neighbor snapshot
        if neighbors:
            await websocket.send_text(json.dumps({
                "type": "neighbors_snapshot",
                "neighbors": [serialize_neighbor(n) for n in neighbors.values()],
            }))

        while True:
            data = await websocket.receive_text()
            try:
                packet = json.loads(data)
            except Exception:
                continue
            if packet.get("type") == "send":
                await handle_send(packet)
            elif packet.get("type") == "send_dm":
                await handle_send_dm(packet)
            elif packet.get("type") == "set_autoadd":
                await handle_set_autoadd(packet)
            elif packet.get("type") == "set_smart_add":
                await handle_set_smart_add(packet)
            elif packet.get("type") == "add_contact":
                await handle_add_contact(packet)
            elif packet.get("type") == "remove_contact":
                await handle_remove_contact(packet)
            elif packet.get("type") == "purge_contacts":
                await handle_purge_contacts()
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)
        logger.info(f"WebSocket client disconnected — total: {len(connected_clients)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MeshCore Channel Web Viewer")
    parser.add_argument(
        "serial_port",
        help="Serial port the MeshCore device is connected to (e.g. /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--baud", type=int, default=115200, help="Serial baud rate (default: 115200)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Web server bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Web server port (default: 8080)"
    )
    args = parser.parse_args()

    serial_port = args.serial_port
    serial_baud = args.baud

    uvicorn.run(app, host=args.host, port=args.port)
