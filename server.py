import asyncio
import argparse
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
    connected_clients -= dead


async def meshcore_listener() -> None:
    logger.info(f"Connecting to MeshCore on {serial_port} at {serial_baud} baud")
    while True:
        try:
            mc = await MeshCore.create_serial(serial_port, serial_baud)
            logger.info(f"Connected to MeshCore on {serial_port}")

            async def on_channel_msg(event) -> None:
                payload = event.payload
                channel_idx = payload.get("channel_idx", 0)
                if channel_idx != 0:
                    return

                # sender_timestamp is Unix seconds from the sender's clock;
                # fall back to server wall-clock if absent.
                ts = payload.get(
                    "sender_timestamp",
                    int(datetime.now(timezone.utc).timestamp()),
                )

                msg = {
                    "type": "message",
                    "text": payload.get("text", ""),
                    "sender": payload.get(
                        "sender",
                        payload.get("pubkey_prefix", "Unknown"),
                    ),
                    "timestamp": ts,
                }
                logger.info(f"Channel 0 message: {msg}")
                await broadcast(msg)

            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel_msg)
            await asyncio.sleep(float("inf"))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"MeshCore error: {exc} — retrying in 5 s")
            await asyncio.sleep(5)


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
        while True:
            # We don't send anything from the browser, but we must keep the
            # receive loop alive so FastAPI detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)
        logger.info(f"WebSocket client disconnected — total: {len(connected_clients)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MeshCore Public Channel Web Viewer")
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
