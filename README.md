<h1>WoL Manager</h1>

A self-hosted Wake-on-LAN management system for broadcast studios and multi-workstation environments.
Control, monitor and automate power management of your entire fleet from a single web interface.

**Note:** Currently supports Windows environments only.

---

## Features

- Wake on LAN — send magic packets to individual devices or entire workstations
- Remote Shutdown — graceful shutdown via lightweight agent
- Real-time Status — live ping monitoring for every device
- Service Monitoring — per-station health indicators for MongoDB, Redis and custom services
- Relay Agent — overcome subnet limitations via always-on relay machines
- Dual-path WoL — relay + direct UDP for maximum reliability
- Scheduled Tasks — automated wake/shutdown by day of week and time
- Production Mode — time-based lock that warns before actions during broadcast hours
- Agent Auto-update — push new agent versions to all machines from the UI with one click
- Dark / Light mode — persistent theme preference
- Persistent Logs — full audit trail stored to disk with timestamps
- Auth support — optional basic authentication layer

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    WoL Manager Server                       │
│              Flask  ·  Python 3  ·  port 5002               │
│                                                             │
│  stations.json ──► station config  (IPs + MACs)             │
│  schedules     ──► APScheduler     (cron-style tasks)       │
│  logs/         ──► rotating logs   (per-day .log files)     │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP / UDP
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
  ┌───────────┐        ┌───────────┐        ┌───────────┐
  │  Relay    │        │ Station 1 │        │ Station 2 │
  │           │        │           │        │           │
  │ wol_agent │        │ wol_agent │        │ wol_agent │
  │  :9876    │        │  :9876    │        │  :9876    │
  └───────────┘        └───────────┘        └───────────┘
```

### WoL Agent (wol_agent.ps1)

A lightweight PowerShell HTTP server running on every managed machine:

| Endpoint | Method | Description |
|---|---|---|
| `/status` | GET | Agent version, hostname, uptime |
| `/wake` | POST | Send WoL magic packet |
| `/shutdown` | POST | Initiate system shutdown |
| `/services` | GET | Query service status |
| `/script` | GET | Serve current agent script (for auto-update) |
| `/update` | POST | Self-update agent from relay source |

---

## Requirements

### Server
- Python 3.8+ (Windows, macOS, Linux)
- Flask
- APScheduler

```bash
pip install -r requirements.txt
```

### Managed Machines
- Windows 10, 11, Server 2016+ (REQUIRED)
- PowerShell 5.1+
- WoL enabled in BIOS
- "Wake on Magic Packet" enabled on the network adapter
- Fast Startup disabled (Control Panel → Power Options → Choose what the power buttons do)

Note: The WoL agent (wol_agent.ps1) only runs on Windows. The Flask server can run on any OS, but all managed machines must be Windows.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/osmelenamorado/wol-manager.git
cd wol-manager
```

### 2. Configure your stations

Copy stations.json.example to stations.json and edit with your network details:

```bash
cp stations.json.example stations.json
```

Edit stations.json with your actual workstations' IP addresses and MAC addresses.

IMPORTANT: Always use the MAC address of the connected NIC.
On machines with two Ethernet adapters, run `ipconfig /all` and identify
the adapter that has an IPv4 address assigned (not Media disconnected).

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. (Optional) Configure authentication

If you want HTTP basic authentication:

```bash
cp auth_config.json.example auth_config.json
```

Edit auth_config.json with your desired credentials. If this file doesn't exist, the UI runs without authentication.

### 5. Start the server

```bat
start.bat
```

Or directly:

```bash
python wol_app.py
```

Open http://<your-server-ip>:5002 in your browser.

---

## WoL Agent Installation Guide

The agent must be installed on every managed machine and on the relay machine.

### Install (run as Administrator in CMD)

```cmd
taskkill /f /im powershell.exe 2>nul
powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\Desktop\wol_agent.ps1" -Install
schtasks /run /tn "WoL Agent"
timeout /t 3
curl http://127.0.0.1:9876/status
```

Expected response (v3.4):
```json
{"status":"running","host":"MACHINE-NAME","version":"3.4","uptime_seconds":5}
```

### Uninstall

```cmd
powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\Desktop\wol_agent.ps1" -Uninstall
```

This removes the scheduled task, kills all agent processes, deletes all artifacts and removes the firewall rule.

### What -Install does automatically

- Kills any previous agent process
- Unregisters the old scheduled task
- Removes leftover copies from all user Desktops
- Clears temporary update scripts
- Copies script to C:\Users\Public\WoLAgent\ (accessible by SYSTEM)
- Creates firewall inbound rule for TCP port 9876
- Registers a new scheduled task (runs at boot + logon, auto-restarts on crash)

---

## Agent Auto-update

Once all machines have v3.4+ installed, you can push future updates from the web UI without touching any machine manually.

### Workflow

1. Copy the new wol_agent.ps1 to C:\Users\Public\WoLAgent\ on the relay machine
2. Open the web UI and click Agents (in header)
3. Click Update All
4. The UI verifies each agent came back with the new version

### How it works

Each agent exposes GET /script which serves its own installed .ps1 as raw bytes.
When /update is called, the agent downloads the new script from the relay's /script endpoint via HTTP (port 9876), backs up the current version, writes the new one, and self-restarts via a detached relaunch script that auto-deletes after use.

---

## Configuration

### stations.json

| Field | Type | Description |
|---|---|---|
| `id` | int | Unique station identifier |
| `name` | string | Display name in the UI |
| `devices[].name` | string | Device label (e.g. SLIDE 01) |
| `devices[].ip` | string | IPv4 address |
| `devices[].mac` | string | MAC address of the connected NIC |

### wol_app.py — Key constants

| Constant | Default | Description |
|---|---|---|
| `AGENT_PORT` | 9876 | Port the WoL agent listens on |
| `AGENT_KEY` | change_me | Shared secret for agent authentication |
| `RELAY_AGENTS` | ["192.168.1.x"] | Always-on machines used as WoL relay |
| `DEFAULT_BROADCAST` | 192.168.1.255 | Subnet broadcast address |
| `PRODUCTION_LOCK_HOUR` | 23 | Hour after which Production Mode deactivates |

### wol_agent.ps1 — Key constants

| Constant | Default | Description |
|---|---|---|
| `$PORT` | 9876 | HTTP listener port |
| `$KEY` | change_me | Must match AGENT_KEY in server |
| `$UPDATE_SRC` | http://192.168.1.100:9876/script | Source URL for auto-updates |
| `$BROADCASTS` | 255.255.255.255, subnet | Broadcast targets for magic packets |

---

## Security Notes

- The AGENT_KEY is a shared secret transmitted in plain HTTP. It is intended for isolated private networks only.
- Do not expose port 5002 or 9876 to the public internet without adding TLS and proper authentication.
- Change AGENT_KEY periodically and update it in both wol_app.py and wol_agent.ps1.
- The key should never appear in documentation, wikis or public repositories.

---

## Project Structure

```
wol-manager/
├── wol_app.py            # Flask server — UI + API
├── wol_agent.ps1         # PowerShell agent (deployed on each machine)
├── stations.json         # Station and device configuration
├── start.bat             # Silent start script (Windows)
├── restart_service.bat   # Restart script (shows progress, auto-closes)
├── README.md             # This file
├── SETUP.md              # Detailed setup guide
├── CHANGELOG.md          # Version history
├── requirements.txt      # Python dependencies
└── logs/                 # Rotating daily log files
    └── wol_YYYY-MM-DD.log
```

---

## UI Overview

### Station Cards
Each card shows:
- Status dot: green (online) / red (offline) / grey (unknown)
- State badge: RUNNING / OFFLINE with time elapsed
- Service indicators: custom services (green / yellow / red)
- Actions: Wake, Off, Ping, Expand

### Header Actions
| Button | Description |
|---|---|
| Wake All | Wake every station simultaneously |
| Shutdown All | Shutdown every online station |
| Agents | View/update agent versions across all machines |
| Schedules | Configure automated wake/shutdown tasks |

### Log Panel
Real-time event stream with colour-coded severity (info / warning / error / success), timestamps and 24h filter.

---

## Troubleshooting

### Machine won't wake up

1. Verify the correct MAC address is in stations.json — run `ipconfig /all` on the target machine and check which adapter has the assigned IP (not Media disconnected)
2. In Device Manager → Network Adapters → connected NIC → Properties:
   - Power Management tab: Allow this device to wake the computer
   - Advanced tab: Wake on Magic Packet = Enabled, Shutdown Wake-On-LAN = Enabled
3. BIOS: Wake on LAN = Enabled
4. Windows: Fast Startup must be disabled

### Agent not responding

```cmd
# Check if agent is running
curl http://127.0.0.1:9876/status

# Check firewall rule exists
netsh advfirewall firewall show rule name="WoL Agent"

# Reinstall cleanly
taskkill /f /im powershell.exe 2>nul
powershell -ExecutionPolicy Bypass -File "wol_agent.ps1" -Install
schtasks /run /tn "WoL Agent"
```

### Agent showing old version after update

An old PowerShell process may still be holding port 9876:

```cmd
taskkill /f /im powershell.exe 2>nul
schtasks /run /tn "WoL Agent"
timeout /t 3
curl http://127.0.0.1:9876/status
```

---

## License

MIT License

Copyright (c) 2026 Osmel Enamorado

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

Made by Osmel Enamorado
