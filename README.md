# MC Hamma

A web-based chat and management interface for [MeshCore](https://github.com/ripplebiz/MeshCore) radio devices. Connect a MeshCore device over serial and get a real-time browser UI for messaging, contact management, and network visualization.

## Features

- **Group & direct messaging** — send and receive messages on any channel; full DM support with per-contact history
- **Contact management** — add, remove, and purge contacts; smart auto-add by seen nodes, relay paths, or active chatters
- **Network graph** — live RF link graph built from observed relay paths, rendered with D3.js
- **Map view** — plot contacts with GPS coordinates on a Leaflet map
- **Advertisement / beacon control** — trigger adverts manually or on a schedule; sparkline history of responses
- **Neighbor view** — see last-hop neighbors with signal info
- **Persistent history** — chat and DM history survive restarts (stored as JSON on disk)
- **Auto-update** — optional systemd timer pulls `origin/main` and restarts the service every 5 minutes

## Requirements

- Python 3.10+
- A MeshCore-compatible device connected via serial (e.g. `/dev/ttyUSB0`)
- Linux with systemd (for the managed service; manual run also works)

## Quick start (manual)

```bash
git clone https://github.com/wbratley/mc-hamma.git
cd mc-hamma
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py /dev/ttyUSB0
```

Then open `http://localhost:8080` in your browser.

### CLI options

```
server.py <serial_port> [--baud BAUD] [--host HOST] [--port PORT]
```

| Option | Default | Description |
|--------|---------|-------------|
| `serial_port` | *(required)* | Serial device path, e.g. `/dev/ttyUSB0` |
| `--baud` | `115200` | Serial baud rate |
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8080` | HTTP port |

## Managed install (systemd)

Run the setup script once as your normal user (not root):

```bash
bash deploy/setup.sh
```

This will:
1. Add your user to the `dialout` group (serial port access)
2. Create a Python venv and install dependencies
3. Create `deploy/mc-hamma.env` from the example file
4. Install and enable `mc-hamma.service` and the `mc-hamma-updater` timer

**Edit the env file** and set your serial port before starting:

```bash
nano deploy/mc-hamma.env   # set MC_SERIAL_PORT=/dev/ttyUSB0
sudo systemctl start mc-hamma.service
sudo systemctl start mc-hamma-updater.timer
```

### Configuration (`deploy/mc-hamma.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MC_SERIAL_PORT` | `/dev/ttyUSB0` | Serial device |
| `MC_BAUD_RATE` | `115200` | Baud rate |
| `MC_HOST` | `0.0.0.0` | Bind address |
| `MC_PORT` | `8080` | HTTP port |

### Useful commands

```bash
sudo systemctl status mc-hamma.service
journalctl -fu mc-hamma          # live app logs
journalctl -fu mc-hamma-updater  # live updater logs
```

## Auto-updater

`mc-hamma-updater.timer` fires every 5 minutes (first run 2 minutes after boot). If `origin/main` has new commits it fast-forward pulls, reinstalls dependencies if `requirements.txt` changed, and restarts the service.

## License

MIT
