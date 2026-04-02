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
HISTORY_MAX  = 1000

connected_clients: Set[WebSocket] = set()
message_buffer: deque = deque(maxlen=HISTORY_MAX)
dm_buffers: dict = {}           # pubkey_prefix -> deque(maxlen=1000) of DM messages
msg_id_counter = count(1)
channel_names: dict = {}    # channel_idx -> name
contacts: dict = {}         # full pubkey (hex) -> {name, lat, lon}
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


NODE_TYPE_NAMES = {0: "Client", 1: "Repeater", 2: "Room Server", 3: "Gateway"}


def serialize_contact(pubkey: str, c: dict) -> dict:
    return {
        "type":         "contact_update",
        "key":          pubkey[:12],
        "name":         c.get("name") or pubkey[:12],
        "node_type":    c.get("node_type"),
        "node_type_name": NODE_TYPE_NAMES.get(c.get("node_type"), "Unknown"),
        "out_path_len": c.get("out_path_len"),
        "last_advert":  c.get("last_advert"),
        "lat":          c.get("lat"),
        "lon":          c.get("lon"),
    }


def build_contacts_snapshot() -> dict:
    return {
        "type":     "contacts_snapshot",
        "contacts": [serialize_contact(k, v) for k, v in contacts.items()],
    }


def serialize_neighbor(n: dict) -> dict:
    snr_hist  = list(n["snr_history"])
    rssi_hist = list(n["rssi_history"])
    contact   = find_contact(n["key"])
    return {
        "type":         "neighbor_update",
        "key":          n["key"],
        "name":         contact.get("name") or n["key"],
        "lat":          contact.get("lat"),
        "lon":          contact.get("lon"),
        "avg_snr":      round(sum(snr_hist)  / len(snr_hist),  1) if snr_hist  else None,
        "avg_rssi":     round(sum(rssi_hist) / len(rssi_hist), 1) if rssi_hist else None,
        "sample_count": len(snr_hist),
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
                }
                message_buffer.append(msg)
                save_history()
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
                        "key":          hop_hash,
                        "snr_history":  deque(maxlen=5),
                        "rssi_history": deque(maxlen=5),
                        "last_seen":    0,
                    }

                n = neighbors[hop_hash]
                n["snr_history"].append(snr)
                if rssi is not None:
                    n["rssi_history"].append(rssi)
                n["last_seen"] = int(datetime.now(timezone.utc).timestamp())

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
            mc.subscribe(EventType.CONTACTS,         on_contact)
            mc.subscribe(EventType.NEW_CONTACT,      on_contact)
            mc.subscribe(EventType.NEXT_CONTACT,     on_contact)

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

            await asyncio.sleep(float("inf"))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"MeshCore error: {exc} — retrying in 5 s")
            mc_instance = None
            await asyncio.sleep(5)


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
