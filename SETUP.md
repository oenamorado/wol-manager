# WoL Manager - Setup Guide

**Important:** WoL Manager currently supports Windows managed machines only. The Flask server can run on any OS, but all devices being managed must be Windows.

---

## Initial Configuration

### 1. Clone and prepare the repository

```bash
git clone https://github.com/oenamorado/wol-manager.git
cd wol-manager
```

### 2. Create configuration files from examples

```bash
cp stations.json.example stations.json
cp auth_config.json.example auth_config.json
```

### 3. Edit `stations.json`

Update with your actual workstations and device details:

```json
{
  "stations": [
    {
      "id": 1,
      "name": "Your Workstation Name",
      "devices": [
        { "name": "Device 1", "ip": "192.168.1.101", "mac": "AA:BB:CC:DD:EE:FF" },
        { "name": "Device 2", "ip": "192.168.1.102", "mac": "AA:BB:CC:DD:EE:FG" }
      ]
    }
  ],
  "schedules": []
}
```

**Finding MAC addresses:**
- On each Windows machine, run: `ipconfig /all`
- Look for the NIC with an assigned IPv4 address (not "Media disconnected")
- Copy the Physical Address (MAC) — the one with colons

### 4. Edit `wol_app.py`

Update these constants to match your network:

```python
DEFAULT_BROADCAST = "192.168.1.255"  # Your subnet broadcast address
PRODUCTION_LOCK_HOUR = 23  # Optional: hour when Production Mode deactivates (0-23)
```

### 5. Edit `wol_agent.ps1` (if using relay)

On the relay/always-on machine, update:

```powershell
$KEY         = "change_me"  # Match AGENT_KEY in wol_app.py
$BROADCASTS  = @("255.255.255.255", "192.168.1.255")  # Your broadcast addresses
$UPDATE_SRC  = "http://192.168.1.100:9876/script"  # Your relay machine IP
```

### 6. Install Python dependencies

```bash
pip install -r requirements.txt
```

Or with venv (recommended):

```bash
python -m venv venv
.\venv\Scripts\activate  # On Windows
source venv/bin/activate  # On macOS/Linux
pip install -r requirements.txt
```

### 7. Start the server

**Using the batch script (Windows):**
```bash
start.bat
```

**Or directly:**
```bash
python wol_app.py
```

The server will be available at `http://localhost:5002`

---

## Deploying the Agent to Managed Machines

### Prerequisites on each machine:

- Windows 10 / 11 / Server 2016+
- PowerShell 5.1+
- Administrator access
- **IMPORTANT:** Fast Startup **disabled**:
  - Control Panel → Power Options → Choose what the power buttons do → Change settings that are currently unavailable
  - Uncheck "Turn on fast startup"

### Network adapter configuration:

For each network adapter you'll use for WoL:

1. Device Manager → Network Adapters
2. Right-click the adapter → Properties
3. **Power Management** tab:
   - ✅ Allow this device to wake the computer
   - ✅ Only allow magic packet to wake the computer

4. **Advanced** tab:
   - Set "Wake on Magic Packet" = **Enabled**
   - Set "Shutdown Wake-On-LAN" = **Enabled**

5. BIOS/UEFI:
   - Enable "Wake on LAN" or "Network Boot"

### Deploy the agent:

On **each managed machine** (including the relay), as Administrator:

```cmd
# Copy wol_agent.ps1 to the machine (via USB, network share, etc.)
# Then:

cd C:\path\to\wol_agent.ps1
powershell -ExecutionPolicy Bypass -File "wol_agent.ps1" -Install
```

Verify installation:

```cmd
curl http://127.0.0.1:9876/status
```

You should see:
```json
{"status":"running","host":"YOUR-MACHINE","version":"3.4","uptime_seconds":...}
```

### Uninstall the agent:

```cmd
powershell -ExecutionPolicy Bypass -File "C:\path\to\wol_agent.ps1" -Uninstall
```

---

## Security Notes

- **AGENT_KEY** is a shared secret transmitted in plain HTTP. Only use on **isolated private networks**.
- **Do NOT expose** port 5002 or 9876 to the public internet without TLS/HTTPS and proper authentication.
- Change `AGENT_KEY` regularly and update it in both `wol_app.py` and `wol_agent.ps1`.
- Never commit `stations.json` or `auth_config.json` with real credentials to version control.

---

## Troubleshooting

### Machine won't wake up

1. Verify the correct MAC address in `stations.json`
2. Check network adapter settings (see "Network adapter configuration" above)
3. Check BIOS has WoL enabled
4. Check Windows Fast Startup is disabled
5. Test directly from relay machine:
   ```powershell
   # On relay, send WoL to target MAC
   $mac = "AA:BB:CC:DD:EE:FF"
   $broadcast = "192.168.1.255"
   # (Use WoL packet sender tool or script)
   ```

### Agent not responding

```cmd
# Check if agent is running
curl http://127.0.0.1:9876/status

# Check firewall rule
netsh advfirewall firewall show rule name="WoL Agent"

# Reinstall
taskkill /f /im powershell.exe 2>nul
powershell -ExecutionPolicy Bypass -File "wol_agent.ps1" -Install
schtasks /run /tn "WoL Agent"
timeout /t 3
curl http://127.0.0.1:9876/status
```

### Server not starting

- Make sure port 5002 is not in use: `netstat -ano | findstr :5002`
- Check Python is installed: `python -version`
- Check dependencies: `pip list | grep -i flask`
- Run with verbose output: `python -u wol_app.py`

---

## Next Steps

1. Start the server and verify all machines show in the UI
2. Test a single wake-up from the UI
3. Test a shutdown
4. Configure schedules for automated wake/shutdown
5. (Optional) Set up a relay machine for machines on different subnets

See the main [README.md](README.md) for full feature documentation.
