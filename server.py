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

connected_clients: Set[WebSocket] = set()
message_buffer: deque = deque(maxlen=50)
msg_id_counter = count(1)
channel_names: dict = {}    # channel_idx -> name
contacts: dict = {}         # full pubkey (hex) -> {name, lat, lon}
neighbors: dict = {}        # last-hop hash (hex) -> neighbor entry
mc_instance = None

# Set at startup from CLI args
serial_port: str = ""
serial_baud: int = 115200


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

                msg = {
                    "id": next(msg_id_counter),
                    "type": "message",
                    "text": text,
                    "sender": sender,
                    "timestamp": ts,
                    "channel_idx": channel_idx,
                    "channel_name": channel_names.get(channel_idx),
                }
                message_buffer.append(msg)
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
                            "name": name,
                            "lat":  c.get("adv_lat"),
                            "lon":  c.get("adv_lon"),
                        }
                        logger.info(f"Contact: {name!r} key={pubkey[:12]}…")

            async def on_rx_log(event) -> None:
                """Track the last-hop node on every received packet."""
                payload        = event.payload
                path           = payload.get("path", "")
                path_hash_size = max(payload.get("path_hash_size", 1), 1)
                snr            = payload.get("snr")
                rssi           = payload.get("rssi")

                if not path or snr is None:
                    return

                # Last entry in path = the node that directly handed us the packet
                chars_per_hop = path_hash_size * 2
                hop_hash      = path[-chars_per_hop:]

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

            mc.subscribe(None, on_any_event)
            mc.subscribe(EventType.CHANNEL_INFO,     on_channel_info)
            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_msg)
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
        logger.info(f"Sent on channel {channel_idx}: {text!r}")
        await broadcast(msg)
    except Exception as exc:
        logger.error(f"Send failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
