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
from fastapi.responses import FileResponse, HTMLResponse
from meshcore import MeshCore, EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

connected_clients: Set[WebSocket] = set()

# Set at startup from CLI args
mc_host: str = "localhost"
mc_port: int = 4000


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
    logger.info(f"Connecting to MeshCore at {mc_host}:{mc_port}")
    while True:
        try:
            mc = await MeshCore.create_tcp(mc_host, mc_port, auto_reconnect=True)
            logger.info("Connected to MeshCore")

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
        "--mc-host", default="localhost", help="MeshCore TCP host (default: localhost)"
    )
    parser.add_argument(
        "--mc-port", type=int, default=4000, help="MeshCore TCP port (default: 4000)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Web server bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Web server port (default: 8080)"
    )
    args = parser.parse_args()

    mc_host = args.mc_host
    mc_port = args.mc_port

    uvicorn.run(app, host=args.host, port=args.port)
