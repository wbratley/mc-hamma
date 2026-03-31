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
channel_names: dict = {}   # channel_idx -> name
nodes: dict = {}           # adv_key -> node entry
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


def serialize_node(node: dict) -> dict:
    snr_hist  = list(node["snr_history"])
    rssi_hist = list(node["rssi_history"])
    return {
        "type": "node_update",
        "key": node["key"],
        "name": node["name"],
        "lat": node["lat"],
        "lon": node["lon"],
        "avg_snr":  round(sum(snr_hist)  / len(snr_hist),  1) if snr_hist  else None,
        "avg_rssi": round(sum(rssi_hist) / len(rssi_hist), 1) if rssi_hist else None,
        "sample_count": len(snr_hist),
        "hops": node["hops"],
        "last_seen": node["last_seen"],
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

            async def on_rx_log(event) -> None:
                payload = event.payload
                # Only interested in ADVERT packets (payload_type 4)
                if payload.get("payload_type") != 4:
                    return
                key = payload.get("adv_key")
                if not key:
                    return

                name = payload.get("adv_name") or key[:12]
                lat  = payload.get("adv_lat")
                lon  = payload.get("adv_lon")
                snr  = payload.get("snr")
                rssi = payload.get("rssi")

                path_len       = payload.get("path_len", 0)
                path_hash_size = max(payload.get("path_hash_size", 1), 1)
                hops = path_len // path_hash_size

                if key not in nodes:
                    nodes[key] = {
                        "key":          key,
                        "name":         name,
                        "lat":          lat,
                        "lon":          lon,
                        "snr_history":  deque(maxlen=5),
                        "rssi_history": deque(maxlen=5),
                        "hops":         hops,
                        "last_seen":    0,
                    }

                node = nodes[key]
                node["name"] = name
                if lat  is not None: node["lat"]  = lat
                if lon  is not None: node["lon"]  = lon
                if snr  is not None: node["snr_history"].append(snr)
                if rssi is not None: node["rssi_history"].append(rssi)
                node["hops"]      = hops
                node["last_seen"] = int(datetime.now(timezone.utc).timestamp())

                logger.info(f"Node seen: {name!r} hops={hops} snr={snr} rssi={rssi} lat={lat} lon={lon}")
                await broadcast(serialize_node(node))

            mc.subscribe(None, on_any_event)
            mc.subscribe(EventType.CHANNEL_INFO,    on_channel_info)
            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_msg)
            mc.subscribe(EventType.RX_LOG_DATA,      on_rx_log)

            await mc.start_auto_message_fetching()

            for idx in range(8):
                try:
                    await mc.commands.get_channel(idx)
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

        # Node snapshot
        if nodes:
            await websocket.send_text(json.dumps({
                "type": "nodes_snapshot",
                "nodes": [serialize_node(n) for n in nodes.values()],
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
