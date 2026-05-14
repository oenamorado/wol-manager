#!/usr/bin/env python3
"""
================================================================================
Wake on LAN Manager
================================================================================

Author: Osmel Enamorado
Copyright (c) 2026 Osmel Enamorado. All rights reserved.

Description:
    Flask application for Wake on LAN, Shutdown and Ping management.
    Supports multiple workstations with SLIDE and STAT devices.

Requirements:
    pip install flask apscheduler

Usage:
    WOL_CLEAR_KEY=your_secret python wol_app.py

================================================================================
"""

import json
import socket
import subprocess
import os
import sys
import re
import time
import base64
import threading
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template_string, jsonify, request, g

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    print("WARNING: APScheduler not installed. Run: pip install apscheduler")

app = Flask(__name__)

# Configuration
STATIONS_FILE = os.path.join(os.path.dirname(__file__), 'stations.json')
LOGS_DIR = os.path.join(os.path.dirname(__file__), 'logs')
AUTH_CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'auth_config.json')
DEFAULT_BROADCAST = "192.168.1.255"  # Change to your subnet broadcast address

PRODUCTION_LOCK_HOUR = 23  # Hour after which Production Mode deactivates (0-23)
CLEAR_KEY = os.environ.get('WOL_CLEAR_KEY', None)  # Set via environment variable

# In-memory storage
activity_logs = []
station_status = {}  # {station_id: {state: 'UNKNOWN'|'OFFLINE'|'BOOTING'|'RUNNING'|'SHUTTING_DOWN'|'PARTIAL', state_since: timestamp}}
scheduled_tasks = []
last_actions = {}
logs_lock = threading.Lock()
status_lock = threading.Lock()

executor = ThreadPoolExecutor(max_workers=20)

scheduler = None
if HAS_APSCHEDULER:
    scheduler = BackgroundScheduler()


def load_auth_users():
    """Load users from auth_config.json (no credentials in code). File not committed."""
    if not os.path.isfile(AUTH_CONFIG_FILE):
        return None
    try:
        with open(AUTH_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        users = data.get('users', [])
        return {u['username']: u['password'] for u in users if u.get('username') and u.get('password')}
    except Exception:
        return None


auth_users = load_auth_users()
if auth_users is None:
    print("WARNING: auth_config.json not found or invalid. Running without HTTP auth (create from auth_config.example.json).")


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_log_file():
    if not os.path.exists(LOGS_DIR):
        os.makedirs(LOGS_DIR)
    return os.path.join(LOGS_DIR, f"wol_{datetime.now().strftime('%Y-%m-%d')}.log")


def normalize_mac(mac: str) -> str:
    s = re.sub(r'[^0-9A-Fa-f]', '', mac)
    if len(s) != 12:
        raise ValueError(f"Invalid MAC: {mac}")
    return s.upper()


def _find_online_agent() -> str:
    """Find any online agent in the network to forward WoL packets."""
    import urllib.request
    stations = load_stations()
    for s in stations:
        for d in s.get('devices', []):
            ip = (d.get('ip') or '').strip()
            if not ip:
                continue
            try:
                req = urllib.request.Request(f"http://{ip}:{AGENT_PORT}/status", method='GET')
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    if data.get('status') == 'running':
                        return ip
            except Exception:
                continue
    return None


def _send_wol_via_agent(agent_ip: str, mac_address: str) -> dict:
    """Ask an online agent to send WoL locally on its subnet."""
    import urllib.request
    
    url = f"http://{agent_ip}:{AGENT_PORT}/wake"
    data = json.dumps({"key": AGENT_KEY, "mac": mac_address}).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        return {"status": "error", "message": "Agent WoL failed"}


def _send_wol_bulk_via_agent(agent_ip: str, mac_list: list) -> dict:
    """Ask an online agent to send WoL for multiple MACs at once."""
    import urllib.request
    
    url = f"http://{agent_ip}:{AGENT_PORT}/wake"
    data = json.dumps({"key": AGENT_KEY, "macs": mac_list}).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        return {"status": "error", "message": "Agent bulk WoL failed"}


# Fixed relay agents — checked in order, first one that responds wins
RELAY_AGENTS = ["10.30.30.119", "10.10.10.34"]

# Cache the last known online agent to avoid scanning every time
_cached_agent_ip = None
_cached_agent_time = 0


def _get_agent_for_wol() -> str:
    """Get an online agent. Checks fixed relays first, then cache, then scans stations."""
    global _cached_agent_ip, _cached_agent_time
    import urllib.request

    def _is_agent_up(ip: str) -> bool:
        try:
            req = urllib.request.Request(f"http://{ip}:{AGENT_PORT}/status", method='GET')
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                return json.loads(resp.read().decode('utf-8')).get('status') == 'running'
        except Exception:
            return False

    # Always try fixed relays first (in order)
    for relay in RELAY_AGENTS:
        if _is_agent_up(relay):
            return relay

    now = time.time()
    # Use cache if less than 30 seconds old
    if _cached_agent_ip and (now - _cached_agent_time) < 30:
        if _is_agent_up(_cached_agent_ip):
            return _cached_agent_ip

    # Scan stations as last resort
    agent = _find_online_agent()
    if agent:
        _cached_agent_ip = agent
        _cached_agent_time = now
    return agent


def send_magic_packet(mac_address: str, broadcast: str = DEFAULT_BROADCAST, ip_address: str = None) -> dict:
    """Send WoL via relay agent AND direct UDP simultaneously for maximum reliability."""
    agent_ok = False
    direct_ok = False

    # METHOD 1: Via relay agent
    agent_ip = _get_agent_for_wol()
    if agent_ip:
        result = _send_wol_via_agent(agent_ip, mac_address)
        agent_ok = result.get('status') == 'success'
        add_log(f"WoL via relay {agent_ip} → {mac_address}: {'OK' if agent_ok else 'FAIL'}", "info")

    # METHOD 2: Direct UDP (always attempted regardless of relay result)
    try:
        mac = normalize_mac(mac_address)
        mac_bytes = bytes.fromhex(mac)
        magic_packet = b'\xff' * 6 + mac_bytes * 16

        targets = [broadcast, '255.255.255.255']
        if ip_address:
            parts = ip_address.split('.')
            targets.insert(0, f"{parts[0]}.{parts[1]}.{parts[2]}.255")

        for _ in range(3):
            for target in targets:
                for port in [9, 7]:
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                            sock.sendto(magic_packet, (target, port))
                    except OSError:
                        pass
        direct_ok = True
        add_log(f"WoL direct UDP → {mac_address}: OK", "info")
    except Exception as e:
        add_log(f"WoL direct UDP → {mac_address}: FAIL ({e})", "warning")

    if agent_ok or direct_ok:
        return {"status": "success", "message": f"WoL sent to {mac_address}"}
    return {"status": "error", "message": f"WoL failed for {mac_address}"}


def ping_host_sync(ip: str, timeout: float = 1) -> dict:
    """Ping host - fast, minimal timeout."""
    try:
        if os.name == "nt":
            cmd = ["ping", "-n", "1", "-w", "500", ip]
        else:
            cmd = ["ping", "-c", "1", "-W", "1", ip]
        
        result = subprocess.run(cmd, capture_output=True, timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        
        online = result.returncode == 0
        return {"status": "success" if online else "info", "online": online}
    except Exception:
        return {"status": "info", "online": False}


def host_is_up(ip: str) -> bool:
    result = ping_host_sync(ip, timeout=2)
    return result.get("online", False)


AGENT_PORT = 9876
AGENT_KEY = "wol_agent_2024"


def shutdown_via_agent(ip_address: str) -> dict:
    """Send shutdown to WoL Agent. Fast timeout."""
    import urllib.request
    
    url = f"http://{ip_address}:{AGENT_PORT}/shutdown"
    data = json.dumps({"key": AGENT_KEY}).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('status') == 'ok':
                return {"status": "success", "message": f"Shutdown sent to {ip_address}"}
            return {"status": "error", "message": result.get('message', 'Error')}
    except Exception:
        return {"status": "error", "message": "Agent unreachable"}


def shutdown_remote(ip_address: str, username: str = None, password: str = None) -> dict:
    """Remote shutdown - uses agent only (fastest and most reliable)."""
    if not ip_address or ip_address.strip() == '':
        return {"status": "error", "message": "No IP"}
    
    ip_address = ip_address.strip()
    
    # Block local shutdown
    if ip_address in ['127.0.0.1', 'localhost', '::1', '0.0.0.0']:
        return {"status": "error", "message": "Cannot shutdown local"}
    
    # Use agent - fastest and works when PC is locked
    return shutdown_via_agent(ip_address)


def load_stations() -> list:
    try:
        with open(STATIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return data.get('stations', [])
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []


def load_schedules() -> list:
    try:
        with open(STATIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data.get('schedules', [])
            return []
    except Exception:
        return []


def save_schedules(schedules: list):
    try:
        with open(STATIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, list):
            data = {"stations": data, "schedules": schedules}
        else:
            data['schedules'] = schedules
        
        with open(STATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving schedules: {e}")


def add_log(message: str, level: str = "info", target: str = None):
    global activity_logs
    with logs_lock:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "level": level,
            "target": target
        }
        activity_logs.append(log_entry)
        
        if target:
            last_actions[target] = {
                "message": message,
                "time": datetime.now().strftime("%H:%M"),
                "level": level
            }
        
        try:
            with open(get_log_file(), 'a', encoding='utf-8') as f:
                f.write(f"{log_entry['timestamp']} [{level.upper()}] {message}\n")
        except OSError:
            pass
        
        cutoff = datetime.now() - timedelta(hours=24)
        activity_logs = [l for l in activity_logs 
                        if datetime.fromisoformat(l["timestamp"]) > cutoff]


def load_logs_from_file():
    global activity_logs
    log_file = get_log_file()
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split(' ', 2)
                    if len(parts) >= 3:
                        timestamp = parts[0]
                        level = parts[1].strip('[]').lower()
                        message = parts[2]
                        activity_logs.append({
                            "timestamp": timestamp,
                            "time": timestamp[11:19] if len(timestamp) > 19 else "00:00:00",
                            "message": message,
                            "level": level,
                            "target": None
                        })
        except OSError:
            pass


def get_logs():
    with logs_lock:
        return list(activity_logs)


def get_station_state(station_id: str) -> dict:
    with status_lock:
        return station_status.get(station_id, {'state': 'UNKNOWN', 'state_since': None})


def set_station_state(station_id: str, state: str):
    with status_lock:
        current = station_status.get(station_id, {})
        if current.get('state') != state:
            station_status[station_id] = {
                'state': state,
                'state_since': datetime.now().isoformat()
            }
        elif station_id not in station_status:
            station_status[station_id] = {
                'state': state,
                'state_since': datetime.now().isoformat()
            }


def validate_wake(ip: str, station_id: str, name: str, max_wait: int = 120):
    def _validate():
        set_station_state(station_id, 'BOOTING')
        add_log(f"{name}: Booting...", "info", station_id)
        
        start = datetime.now()
        while (datetime.now() - start).seconds < max_wait:
            if host_is_up(ip):
                elapsed = (datetime.now() - start).seconds
                set_station_state(station_id, 'RUNNING')
                add_log(f"{name}: Online in {elapsed}s", "success", station_id)
                return True
            threading.Event().wait(3)
        
        set_station_state(station_id, 'UNKNOWN')
        add_log(f"{name}: Boot timeout ({max_wait}s)", "warning", station_id)
        return False
    
    executor.submit(_validate)


def validate_shutdown(ip: str, station_id: str, name: str, max_wait: int = 60):
    def _validate():
        set_station_state(station_id, 'SHUTTING_DOWN')
        add_log(f"{name}: Shutting down...", "info", station_id)
        
        start = datetime.now()
        while (datetime.now() - start).seconds < max_wait:
            if not host_is_up(ip):
                elapsed = (datetime.now() - start).seconds
                set_station_state(station_id, 'OFFLINE')
                add_log(f"{name}: Offline in {elapsed}s", "success", station_id)
                return True
            threading.Event().wait(3)
        
        add_log(f"{name}: Shutdown timeout - may still be running", "error", station_id)
        return False
    
    executor.submit(_validate)


def execute_scheduled_task(action: str, target: str):
    stations = load_stations()
    
    if action == 'wake':
        if target == 'all':
            for s in stations:
                for d in s.get('devices', []):
                    mac = d.get('mac', '')
                    if mac:
                        send_magic_packet(mac, ip_address=d.get('ip', ''))
            add_log("Scheduled: Wake All executed", "success")
        else:
            for s in stations:
                if str(s.get('id')) == str(target):
                    for d in s.get('devices', []):
                        mac = (d.get('mac') or '').strip()
                        if mac:
                            send_magic_packet(mac, ip_address=d.get('ip', ''))
                            device_id = f"{s.get('id')}-{d.get('name')}"
                            if d.get('ip'):
                                validate_wake(d.get('ip'), device_id, d.get('name'))
                    break
    elif action == 'shutdown':
        if target == 'all':
            for s in stations:
                for d in s.get('devices', []):
                    if d.get('ip'):
                        shutdown_remote(d.get('ip', ''))
            add_log("Scheduled: Shutdown All executed", "warning")
        else:
            for s in stations:
                if str(s.get('id')) == str(target):
                    for d in s.get('devices', []):
                        ip = (d.get('ip') or '').strip()
                        if ip:
                            shutdown_remote(ip)
                            device_id = f"{s.get('id')}-{d.get('name')}"
                            validate_shutdown(ip, device_id, d.get('name'))
                    break


def setup_scheduler():
    if not HAS_APSCHEDULER or not scheduler:
        return
    
    scheduler.remove_all_jobs()
    
    schedules = load_schedules()
    for i, task in enumerate(schedules):
        days_map = {0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun'}
        days_str = ','.join([days_map[d] for d in task.get('days', [])])
        
        if days_str and task.get('time'):
            hour, minute = task['time'].split(':')
            trigger = CronTrigger(day_of_week=days_str, hour=int(hour), minute=int(minute))
            scheduler.add_job(
                execute_scheduled_task,
                trigger,
                args=[task.get('action'), task.get('target')],
                id=f"task_{i}"
            )
    
    if not scheduler.running:
        scheduler.start()


previous_status = {}

def check_all_status_async():
    stations = load_stations()
    
    def check_single_device(item_id, ip, name):
        online = host_is_up(ip)
        current_state = get_station_state(item_id)
        
        # Don't override transitional states
        if current_state.get('state') in ['BOOTING', 'SHUTTING_DOWN']:
            return {'id': item_id, 'online': online, 'state': current_state.get('state'), 'state_since': current_state.get('state_since')}
        
        new_state = 'RUNNING' if online else 'OFFLINE'
        prev = previous_status.get(item_id)
        
        if prev != new_state:
            previous_status[item_id] = new_state
            set_station_state(item_id, new_state)
            if prev is not None:
                add_log(f"{name}: {'Online' if online else 'Offline'}", 
                       "success" if online else "warning", item_id)
        
        state_info = get_station_state(item_id)
        return {'id': item_id, 'online': online, 'state': state_info.get('state', new_state), 'state_since': state_info.get('state_since')}
    
    # First, check all devices
    device_futures = []
    device_mapping = {}  # station_id -> list of device futures
    
    for s in stations:
        station_id = s.get('id')
        device_mapping[station_id] = []
        for d in s.get('devices', []):
            device_id = f"{station_id}-{d.get('name')}"
            future = executor.submit(check_single_device, device_id, d.get('ip'), d.get('name'))
            device_futures.append(future)
            device_mapping[station_id].append((device_id, future))
    
    # Wait for all device checks to complete
    for f in device_futures:
        f.result()
    
    # Now derive station status from devices
    for s in stations:
        station_id = s.get('id')
        station_name = s.get('name')
        devices = device_mapping.get(station_id, [])
        
        if not devices:
            # No devices, check station IP directly
            online = host_is_up(s.get('ip'))
            new_state = 'RUNNING' if online else 'OFFLINE'
        else:
            # Derive from devices
            device_states = []
            for device_id, future in devices:
                result = future.result()
                device_states.append(result.get('state') == 'RUNNING')
            
            online_count = sum(device_states)
            total_count = len(device_states)
            
            if online_count == total_count:
                new_state = 'RUNNING'  # All devices online
            elif online_count > 0:
                new_state = 'PARTIAL'  # Some devices online
            else:
                new_state = 'OFFLINE'  # All devices offline
        
        current_state = get_station_state(station_id)
        
        # Don't override transitional states
        if current_state.get('state') in ['BOOTING', 'SHUTTING_DOWN']:
            continue
        
        prev = previous_status.get(station_id)
        
        if prev != new_state:
            previous_status[station_id] = new_state
            set_station_state(station_id, new_state)
            if prev is not None:
                if new_state == 'RUNNING':
                    add_log(f"{station_name}: All devices online", "success", station_id)
                elif new_state == 'PARTIAL':
                    add_log(f"{station_name}: Partial - some devices offline", "warning", station_id)
                else:
                    add_log(f"{station_name}: All devices offline", "error", station_id)
    
    return []


# ============================================================================
# HTML TEMPLATE
# ============================================================================

HTML_TEMPLATE = '''
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>WoL Manager</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: { extend: { fontFamily: { sans: ['Inter', 'sans-serif'] } } }
        }
    </script>
    <style>
        :root {
            --bg: #f8fafc;
            --card: rgba(255,255,255,0.7);
            --card-solid: #ffffff;
            --border: rgba(0,0,0,0.06);
            --text: #0f172a;
            --text-2: #475569;
            --text-3: #94a3b8;
        }
        .dark {
            --bg: #0f172a;
            --card: rgba(30,41,59,0.7);
            --card-solid: #1e293b;
            --border: rgba(255,255,255,0.08);
            --text: #f1f5f9;
            --text-2: #94a3b8;
            --text-3: #64748b;
        }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); }
        
        .glass { background: var(--card); backdrop-filter: blur(16px); border: 1px solid var(--border); }
        .glass-solid { background: var(--card-solid); border: 1px solid var(--border); }
        
        /* Extended status dots with 6 states including PARTIAL */
        .status-dot {
            width: 10px; height: 10px; border-radius: 50%;
            background: #6b7280;
            flex-shrink: 0;
        }
        .status-dot.status-running {
            background: #22c55e;
            animation: pulse-green 2s infinite;
        }
        .status-dot.status-partial {
            background: #f59e0b;
            animation: pulse-yellow 2s infinite;
        }
        .status-dot.status-booting {
            background: #eab308;
            animation: pulse-yellow 1s infinite;
        }
        .status-dot.status-shutting-down {
            background: #f97316;
            animation: pulse-orange 1s infinite;
        }
        .status-dot.status-offline {
            background: #ef4444;
        }
        .status-dot.status-unknown {
            background: #6b7280;
        }
        
        @keyframes pulse-green {
            0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.5); }
            50% { box-shadow: 0 0 0 6px rgba(34, 197, 94, 0); }
        }
        @keyframes pulse-yellow {
            0%, 100% { box-shadow: 0 0 0 0 rgba(234, 179, 8, 0.5); }
            50% { box-shadow: 0 0 0 6px rgba(234, 179, 8, 0); }
        }
        @keyframes pulse-orange {
            0%, 100% { box-shadow: 0 0 0 0 rgba(249, 115, 22, 0.5); }
            50% { box-shadow: 0 0 0 6px rgba(249, 115, 22, 0); }
        }
        
        /* Status badge styles including PARTIAL */
        .status-badge {
            font-size: 10px;
            padding: 2px 8px;
            border-radius: 9999px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .status-badge.status-running {
            background: rgba(34, 197, 94, 0.15);
            color: #22c55e;
        }
        .status-badge.status-partial {
            background: rgba(245, 158, 11, 0.15);
            color: #f59e0b;
        }
        .status-badge.status-booting {
            background: rgba(234, 179, 8, 0.15);
            color: #eab308;
        }
        .status-badge.status-shutting-down {
            background: rgba(249, 115, 22, 0.15);
            color: #f97316;
        }
        .status-badge.status-offline {
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
        }
        .status-badge.status-unknown {
            background: rgba(107, 114, 128, 0.15);
            color: #6b7280;
        }
        
        .last-action {
            font-size: 10px;
            padding: 2px 8px;
            border-radius: 4px;
            background: rgba(0,0,0,0.04);
        }
        .dark .last-action { background: rgba(255,255,255,0.04); }
        
        .btn-action {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 12px; border-radius: 10px;
            font-size: 12px; font-weight: 500;
            transition: all 0.15s; cursor: pointer;
        }
        .btn-action:hover { transform: translateY(-1px); }
        .btn-action.wake { background: rgba(34,197,94,0.1); color: #22c55e; }
        .btn-action.wake:hover { background: rgba(34,197,94,0.2); }
        .btn-action.ping { background: rgba(59,130,246,0.1); color: #3b82f6; }
        .btn-action.ping:hover { background: rgba(59,130,246,0.2); }
        .btn-action.off { background: rgba(239,68,68,0.1); color: #ef4444; }
        .btn-action.off:hover { background: rgba(239,68,68,0.2); }
        
        .btn-icon { 
            width: 36px; height: 36px; 
            display: inline-flex; align-items: center; justify-content: center; 
            border-radius: 10px; font-size: 14px; 
            transition: all 0.15s; cursor: pointer; 
        }
        .btn-icon:hover { transform: translateY(-1px); }
        
        .btn-header {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 14px; border-radius: 10px;
            font-size: 13px; font-weight: 500;
            transition: all 0.15s; cursor: pointer;
        }
        .btn-wake { background: rgba(34,197,94,0.1); color: #22c55e; }
        .btn-wake:hover { background: rgba(34,197,94,0.2); }
        .btn-off { background: rgba(239,68,68,0.1); color: #ef4444; }
        .btn-off:hover { background: rgba(239,68,68,0.2); }
        
        .input-glass {
            width: 100%;
            padding: 12px 16px;
            border-radius: 12px;
            font-size: 14px;
            background: var(--card);
            backdrop-filter: blur(8px);
            border: 1px solid var(--border);
            color: var(--text);
            outline: none;
            transition: all 0.2s;
            -webkit-appearance: none;
            appearance: none;
        }
        .input-glass:focus {
            border-color: rgba(99, 102, 241, 0.5);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }
        .input-glass option {
            background: var(--card-solid);
            color: var(--text);
            padding: 8px;
        }
        
        .day-checkbox {
            width: 40px; height: 40px;
            border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer;
            font-size: 13px; font-weight: 600;
            background: var(--card);
            backdrop-filter: blur(8px);
            border: 1px solid var(--border);
            color: var(--text-2);
            transition: all 0.2s;
        }
        .day-checkbox:hover { border-color: rgba(99, 102, 241, 0.3); }
        .day-checkbox.selected {
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            border-color: transparent;
            color: white;
        }
        
        .log-item { 
            padding: 8px 10px; border-radius: 8px; font-size: 11px; 
            background: rgba(0,0,0,0.03); border-left: 3px solid transparent;
            display: flex; gap: 10px;
        }
        .dark .log-item { background: rgba(255,255,255,0.03); }
        .log-success { border-left-color: #22c55e; }
        .log-error { border-left-color: #ef4444; }
        .log-warning { border-left-color: #f59e0b; }
        .log-info { border-left-color: #3b82f6; }
        
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 100; opacity: 0; visibility: hidden; transition: all 0.2s; }
        .modal.open { opacity: 1; visibility: visible; }
        .modal-box { 
            background: var(--card); 
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 20px; 
            padding: 28px; 
            width: 420px; 
            max-width: 90%; 
            transform: scale(0.95); 
            transition: transform 0.2s;
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25);
        }
        .modal.open .modal-box { transform: scale(1); }
        
        .confirm-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 150; opacity: 0; visibility: hidden; transition: all 0.2s; }
        .confirm-modal.open { opacity: 1; visibility: visible; }
        .confirm-box {
            background: var(--card);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 24px;
            width: 400px;
            max-width: 90%;
            transform: scale(0.95);
            transition: transform 0.2s;
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25);
            text-align: center;
        }
        .confirm-modal.open .confirm-box { transform: scale(1); }
        
        .task-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 14px;
            border-radius: 12px;
            background: var(--card);
            backdrop-filter: blur(8px);
            border: 1px solid var(--border);
        }
        .task-delete {
            width: 32px; height: 32px;
            display: flex; align-items: center; justify-content: center;
            border-radius: 8px;
            color: #ef4444;
            background: rgba(239,68,68,0.1);
            cursor: pointer;
            transition: all 0.15s;
        }
        .task-delete:hover { background: rgba(239,68,68,0.2); }
        
        .svc-ind {
            display: inline-flex; align-items: center; gap: 5px;
            font-size: 11px; font-weight: 600; padding: 4px 9px;
            border-radius: 7px; text-transform: uppercase; letter-spacing: 0.4px;
        }
        .svc-ind i { font-size: 10px; }
        .svc-green   { background: rgba(34,197,94,0.12);  color: #22c55e; }
        .svc-yellow  { background: rgba(245,158,11,0.12); color: #f59e0b; }
        .svc-red     { background: rgba(239,68,68,0.12);  color: #ef4444; }
        .svc-unknown { background: rgba(107,114,128,0.08); color: #4b5563; }

        ::-webkit-scrollbar { width: 0; height: 0; display: none; }
        ::-webkit-scrollbar-thumb { background: transparent; }
        * { scrollbar-width: none; -ms-overflow-style: none; }
        
        .dropdown-content {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            margin-top: 8px;
            z-index: 40;
            opacity: 0;
            visibility: hidden;
            transform: translateY(-8px);
            transition: all 0.2s ease;
            pointer-events: none;
        }
        .station-card.open .dropdown-content {
            opacity: 1;
            visibility: visible;
            transform: translateY(0);
            pointer-events: auto;
        }
        
        .dropdown-toggle {
            width: 32px; height: 32px;
            display: flex; align-items: center; justify-content: center;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
            background: rgba(0,0,0,0.03);
        }
        .dark .dropdown-toggle { background: rgba(255,255,255,0.03); }
        .dropdown-toggle:hover { background: rgba(0,0,0,0.06); }
        .dark .dropdown-toggle:hover { background: rgba(255,255,255,0.06); }
        
        .chip {
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 600;
        }
        .chip-slide { background: rgba(59,130,246,0.15); color: #3b82f6; }
        .chip-stat { background: rgba(168,85,247,0.15); color: #a855f7; }
        
        /* State badge with time */
        .state-badge {
            font-size: 10px;
            padding: 3px 8px;
            border-radius: 6px;
            font-weight: 600;
            text-transform: uppercase;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .state-running { background: rgba(34,197,94,0.15); color: #22c55e; }
        .state-booting { background: rgba(234,179,8,0.15); color: #eab308; }
        .state-shutting_down { background: rgba(249,115,22,0.15); color: #f97316; }
        .state-offline { background: rgba(239,68,68,0.15); color: #ef4444; }
        .state-unknown { background: rgba(148,163,184,0.15); color: #94a3b8; }
        .state-partial { background: rgba(245,158,11,0.15); color: #f59e0b; }
        
        .state-time {
            font-weight: 400;
            opacity: 0.8;
            font-size: 9px;
        }
        
        .lock-indicator {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 10px;
            padding: 4px 8px;
            border-radius: 6px;
            background: rgba(234,179,8,0.15);
            color: #eab308;
        }
        
        /* Bulk results */
        .bulk-result {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 12px;
            background: rgba(0,0,0,0.03);
            margin-bottom: 6px;
        }
        .dark .bulk-result { background: rgba(255,255,255,0.03); }
        .bulk-ok { color: #22c55e; }
        .bulk-fail { color: #ef4444; }
    </style>
</head>
<body class="min-h-screen">
    <script>
        if (localStorage.theme === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
            document.documentElement.classList.add('dark');
        }
    </script>
    
    <!-- Fixed header -->
    <header class="glass fixed top-0 left-0 right-0 z-50 h-14">
        <div class="h-full px-6 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center text-white">
                    <i class="fa-solid fa-bolt text-sm"></i>
                </div>
                <div>
                    <span class="font-semibold leading-tight block" style="color: var(--text)">WoL Manager</span>
                    <span class="text-[10px] leading-tight" style="color: var(--text-3)">by Osmel Enamorado</span>
                </div>
                <div id="lock-indicator" class="lock-indicator" style="display: none;">
                    <i class="fa-solid fa-lock text-[9px]"></i>
                    <span>Production Mode</span>
                </div>
            </div>
            
            <div class="flex items-center gap-2">
                <button onclick="toggleTheme()" class="btn-icon" style="background: var(--card); color: var(--text-2)">
                    <i class="fa-solid fa-moon dark:hidden"></i>
                    <i class="fa-solid fa-sun hidden dark:block"></i>
                </button>
                
                <div class="w-px h-6 mx-2" style="background: var(--border)"></div>
                
                <!-- Wake All now has confirmation -->
                <button onclick="confirmWakeAll()" class="btn-header btn-wake">
                    <i class="fa-solid fa-bolt"></i>
                    <span>Wake All</span>
                </button>
                <button onclick="confirmShutdownAll()" class="btn-header btn-off">
                    <i class="fa-solid fa-power-off"></i>
                    <span>Shutdown All</span>
                </button>
                
                <button onclick="openAgentsModal()" class="btn-icon" style="background: var(--card); color: var(--text-2)" title="Agents">
                    <i class="fa-solid fa-microchip"></i>
                </button>
                <button onclick="openModal()" class="btn-icon" style="background: var(--card); color: var(--text-2)">
                    <i class="fa-regular fa-clock"></i>
                </button>
            </div>
        </div>
    </header>
    
<!-- Main -->
  <main class="pt-14 h-screen flex">
  <!-- Stations column - 80% -->
  <div class="flex-1 p-6 overflow-y-auto">
  <div class="space-y-4 pb-6">
                {% for station in stations %}
                <div class="station-card relative" data-station-id="{{ station.id }}">
                    <div class="glass-solid rounded-2xl p-5">
                        <div class="grid items-center" style="grid-template-columns: 1fr auto 1fr;">
                            <div class="flex items-center gap-4">
                                <!-- Use the new status-dot class -->
                                <div class="status-dot status-unknown" id="status-{{ station.id }}"></div>
                                <div>
                                    <div class="flex items-center gap-3">
                                        <h3 class="font-semibold" style="color: var(--text)">{{ station.name }}</h3>
                                        <!-- State badge now includes time since -->
                                        <!-- Use the new status-badge class -->
                                        <span class="status-badge status-unknown" id="state-badge-{{ station.id }}">
                                            <span class="state-label">Unknown</span>
                                            <span class="state-time" id="state-time-{{ station.id }}"></span>
                                        </span>
                                    </div>
                                    <p class="text-xs font-mono mt-0.5" style="color: var(--text-3)">{% if station.devices %}{{ station.devices|length }} devices · {{ station.devices|map(attribute='name')|join(', ') }}{% else %}{{ station.ip or '' }} · {{ station.mac or '' }}{% endif %}</p>
                                </div>
                            </div>

                            <!-- Services — center -->
                            <div class="flex items-center gap-2 justify-center">
                                <div class="svc-ind svc-unknown" id="svc-mongodb-{{ station.id }}"><i class="fa-solid fa-database"></i>MongoDB</div>
                                <div class="svc-ind svc-unknown" id="svc-redis-{{ station.id }}"><i class="fa-solid fa-bolt"></i>Redis</div>
                                <div class="svc-ind svc-unknown" id="svc-wtvision-{{ station.id }}"><i class="fa-solid fa-tv"></i>WTVision</div>
                            </div>

                            <div class="flex items-center gap-2 justify-end">
                                {% if station.devices %}
                                <button onclick="wakeStation('{{ station.id }}', '{{ station.name }}')" class="btn-action wake" title="Wake all devices">
                                    <i class="fa-solid fa-bolt"></i>
                                    <span>Wake</span>
                                </button>
                                <button onclick="confirmShutdown('{{ station.devices[0].ip }}', '{{ station.name }}', '{{ station.id }}', '{{ station.id }}')" class="btn-action off" title="Shutdown Station + Devices">
                                    <i class="fa-solid fa-power-off"></i>
                                    <span>Off</span>
                                </button>
                                {% else %}
                                <button onclick="wakeup('{{ station.mac or '' }}', '{{ station.ip or '' }}', '{{ station.name }}', '{{ station.id }}')" class="btn-action wake" title="Wake">
                                    <i class="fa-solid fa-bolt"></i>
                                    <span>Wake</span>
                                </button>
                                <button onclick="confirmShutdown('{{ station.ip or '' }}', '{{ station.name }}', '{{ station.id }}', '{{ station.id }}')" class="btn-action off" title="Shutdown Station + Devices">
                                    <i class="fa-solid fa-power-off"></i>
                                    <span>Off</span>
                                </button>
                                {% endif %}
                                
                                {% if station.devices %}
                                <button onclick="pingHost('{{ station.devices[0].ip }}', '{{ station.name }}', '{{ station.id }}')" class="btn-icon" style="background: rgba(59,130,246,0.1); color: #3b82f6;" title="Ping">
                                    <i class="fa-solid fa-wifi text-xs"></i>
                                </button>
                                <div class="w-px h-6 mx-1" style="background: var(--border)"></div>
                                <div class="dropdown-toggle" onclick="toggleDropdown('{{ station.id }}')">
                                    <i class="fa-solid fa-chevron-down text-xs transition-transform duration-200 dropdown-arrow" style="color: var(--text-3)"></i>
                                </div>
                                {% endif %}
                            </div>
                        </div>
                    </div>
                    
                    {% if station.devices %}
                    <div class="dropdown-content">
                        <div class="glass-solid rounded-2xl p-4 shadow-xl">
                            <div class="grid grid-cols-2 gap-3">
                                {% for device in station.devices %}
                                <div class="p-4 rounded-xl" style="background: rgba(0,0,0,0.03)">
                                    <div class="flex items-center justify-between mb-3">
                                        <div class="flex items-center gap-2">
                                            <!-- Use the new status-dot class -->
                                            <div class="status-dot status-unknown" id="device-status-{{ station.id }}-{{ device.name }}"></div>
                                            <span class="chip {% if device.name == 'SLIDE' %}chip-slide{% else %}chip-stat{% endif %}">{{ device.name }}</span>
                                        </div>
                                        <span class="last-action" id="last-action-{{ station.id }}-{{ device.name }}" style="color: var(--text-3); display: none; font-size: 9px;"></span>
                                    </div>
                                    <p class="text-[11px] font-mono mb-3" style="color: var(--text-3)">{{ device.ip }}</p>
                                    <div class="flex gap-2">
                                        <button onclick="event.stopPropagation(); wakeup('{{ device.mac }}', '{{ device.ip }}', '{{ device.name }}', '{{ station.id }}-{{ device.name }}')" class="btn-action wake" style="padding:6px 10px;font-size:11px">
                                            <i class="fa-solid fa-bolt"></i>
                                            <span>Wake</span>
                                        </button>
                                        <button onclick="event.stopPropagation(); pingHost('{{ device.ip }}', '{{ device.name }}', '{{ station.id }}-{{ device.name }}')" class="btn-action ping" style="padding:6px 10px;font-size:11px">
                                            <i class="fa-solid fa-wifi"></i>
                                            <span>Ping</span>
                                        </button>
                                        <button onclick="event.stopPropagation(); confirmShutdown('{{ device.ip }}', '{{ device.name }}', '{{ station.id }}-{{ device.name }}')" class="btn-action off" style="padding:6px 10px;font-size:11px">
                                            <i class="fa-solid fa-power-off"></i>
                                            <span>Off</span>
                                        </button>
                                    </div>
                                </div>
                                {% endfor %}
                            </div>
                        </div>
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
                
                {% if not stations %}
                <div class="glass-solid rounded-2xl p-12 text-center">
                    <i class="fa-solid fa-server text-4xl mb-4" style="color: var(--text-3)"></i>
                    <p class="font-medium" style="color: var(--text)">No stations found</p>
                    <p class="text-sm mt-1" style="color: var(--text-3)">Configure stations.json</p>
                </div>
                {% endif %}
            </div>
        </div>
        
        <!-- Logs panel - 20% -->
        <aside class="w-[20%] min-w-[280px] p-6 pl-0">
            <div class="glass-solid rounded-2xl p-5 sticky top-20 h-[calc(100vh-6rem)]">
<div class="flex items-center justify-between mb-4">
  <h4 class="font-semibold" style="color: var(--text)">Logs</h4>
  <div class="flex items-center gap-2">
    <span class="text-[10px] px-2 py-1 rounded-md" style="background: rgba(0,0,0,0.05); color: var(--text-3)">24h</span>
    {% if is_admin %}
    <button onclick="showClearModal()" class="text-[10px] px-2 py-1 rounded-md hover:bg-red-500/20 transition-colors" style="color: #ef4444;" title="Clear Logs">
      <i class="fas fa-trash-alt"></i>
    </button>
    {% endif %}
  </div>
  </div>
                <div id="logs-box" class="space-y-2 overflow-y-auto" style="height: calc(100% - 40px)">
                    <div class="log-item log-info">
                        <span style="color: var(--text-3)">--:--</span>
                        <span style="color: var(--text-2)">System started</span>
                    </div>
                </div>
            </div>
        </aside>
    </main>
    
    <!-- Combined confirm modal for shutdown and wake all -->
    <div id="confirm-modal" class="confirm-modal" onclick="if(event.target===this)closeConfirm()">
        <div class="confirm-box">
            <div class="w-14 h-14 mx-auto mb-4 rounded-full flex items-center justify-center" id="confirm-icon-container" style="background: rgba(239,68,68,0.1);">
                <i class="fa-solid fa-power-off text-xl text-red-500" id="confirm-icon"></i>
            </div>
            <h3 class="font-semibold text-lg mb-2" style="color: var(--text)" id="confirm-title">Confirm Shutdown</h3>
            <p class="text-sm mb-3" style="color: var(--text-2)"><span id="confirm-action-text">Shutdown</span> <strong id="confirm-target"></strong>?</p>
            
            <!-- Double confirm checkbox -->
            <div id="double-confirm-container" class="mb-4 p-3 rounded-xl text-left" style="background: rgba(234,179,8,0.1); display: none;">
                <label class="flex items-start gap-3 cursor-pointer">
                    <input type="checkbox" id="double-confirm-check" class="mt-1">
                    <span class="text-xs" style="color: var(--text-2)">
                        <strong class="text-yellow-600" id="confirm-warning-text">Production Mode Active</strong><br>
                        <span id="confirm-warning-desc">I confirm this shutdown during production hours</span>
                    </span>
                </label>
            </div>
            
            <!-- Bulk results container -->
            <div id="bulk-results" class="mb-4 max-h-48 overflow-y-auto text-left" style="display: none;"></div>
            
            <div class="flex gap-3" id="confirm-buttons">
                <button onclick="closeConfirm()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium" style="background: var(--card); border: 1px solid var(--border); color: var(--text-2)">
                    Cancel
                </button>
                <button id="confirm-action-btn" onclick="executeConfirmedAction()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium text-white" style="background: #ef4444;">
                    Shutdown
                </button>
            </div>
        </div>
    </div>
    
    <!-- Schedule modal -->
    <div id="modal" class="modal" onclick="if(event.target===this)closeModal()">
        <div class="modal-box">
            <div class="flex items-center justify-between mb-6">
                <h3 class="font-semibold text-lg" style="color: var(--text)">Schedule Task</h3>
                <button onclick="closeModal()" class="btn-icon" style="background: rgba(0,0,0,0.05); border: 1px solid var(--border); color: var(--text-2)">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>
            
            <form id="schedule-form" onsubmit="saveSchedule(event)" class="space-y-5">
                <div>
                    <label class="block text-sm font-medium mb-2" style="color: var(--text-2)">Action</label>
                    <select name="action" required class="input-glass">
                        <option value="wake">Wake (Wake on LAN)</option>
                        <option value="shutdown">Shutdown</option>
                    </select>
                </div>
                
                <div>
                    <label class="block text-sm font-medium mb-2" style="color: var(--text-2)">Target</label>
                    <select name="target" required class="input-glass">
                        <option value="all">All stations</option>
                        {% for station in stations %}
                        <option value="{{ station.id }}">{{ station.name }}</option>
                        {% endfor %}
                    </select>
                </div>
                
                <div>
                    <label class="block text-sm font-medium mb-2" style="color: var(--text-2)">Time</label>
                    <input type="time" name="time" required class="input-glass">
                </div>
                
                <div>
                    <label class="block text-sm font-medium mb-3" style="color: var(--text-2)">Days of the week</label>
                    <div class="flex gap-2" id="days-selector">
                        <div class="day-checkbox" data-day="0" onclick="toggleDay(this)">M</div>
                        <div class="day-checkbox" data-day="1" onclick="toggleDay(this)">T</div>
                        <div class="day-checkbox" data-day="2" onclick="toggleDay(this)">W</div>
                        <div class="day-checkbox" data-day="3" onclick="toggleDay(this)">T</div>
                        <div class="day-checkbox" data-day="4" onclick="toggleDay(this)">F</div>
                        <div class="day-checkbox" data-day="5" onclick="toggleDay(this)">S</div>
                        <div class="day-checkbox" data-day="6" onclick="toggleDay(this)">S</div>
                    </div>
                </div>
                
                <div class="flex gap-3 pt-3">
                    <button type="button" onclick="closeModal()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium" style="background: var(--card); border: 1px solid var(--border); color: var(--text-2)">
                        Cancel
                    </button>
                    <button type="submit" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium text-white" style="background: linear-gradient(135deg, #6366f1, #8b5cf6);">
                        Save
                    </button>
                </div>
            </form>
            
            <div class="mt-6 pt-5" style="border-top: 1px solid var(--border)">
                <h4 class="text-sm font-medium mb-3" style="color: var(--text-2)">Scheduled Tasks</h4>
                <div id="tasks-list" class="space-y-2 max-h-48 overflow-y-auto" style="color: var(--text-3)">Loading...</div>
            </div>
        </div>
    </div>
    
    <!-- Agents (versions + update) modal -->
    <div id="agents-modal" class="modal" onclick="if(event.target===this)closeAgentsModal()">
        <div class="modal-box" style="max-width: 640px;">
            <!-- Header -->
            <div class="flex items-center justify-between mb-4">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl flex items-center justify-center" style="background: rgba(59,130,246,0.12);">
                        <i class="fa-solid fa-microchip" style="color: #3b82f6"></i>
                    </div>
                    <div>
                        <h3 class="font-semibold text-lg leading-tight" style="color: var(--text)">Agents</h3>
                        <p class="text-xs" style="color: var(--text-2)">Manage remote agent versions</p>
                    </div>
                </div>
                <button onclick="closeAgentsModal()" class="btn-icon" style="background: rgba(0,0,0,0.05); border: 1px solid var(--border); color: var(--text-2)">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>

            <!-- Source -->
            <div class="rounded-xl px-3 py-2 mb-3 text-xs flex items-center gap-2"
                 style="background: var(--card); border: 1px solid var(--border); color: var(--text-2);">
                <i class="fa-solid fa-folder-tree" style="color: var(--text-3)"></i>
                <span>Source:</span>
                <code style="color: var(--text); font-size: 11px;">http://10.30.30.119:9876/script</code>
            </div>

            <!-- Actions -->
            <div class="flex items-center justify-between mb-3">
                <span class="text-xs" id="agents-summary" style="color: var(--text-3)">—</span>
                <div class="flex gap-2">
                    <button onclick="refreshAgents()" class="px-3 py-1.5 rounded-lg text-xs font-medium inline-flex items-center gap-1.5"
                            style="background: var(--card); border: 1px solid var(--border); color: var(--text-2)">
                        <i class="fa-solid fa-rotate"></i> Refresh
                    </button>
                    <button onclick="updateAllAgents()" class="px-3 py-1.5 rounded-lg text-xs font-medium text-white inline-flex items-center gap-1.5"
                            style="background: linear-gradient(135deg, #3b82f6, #2563eb);">
                        <i class="fa-solid fa-cloud-arrow-down"></i> Update All
                    </button>
                </div>
            </div>

            <!-- List -->
            <div id="agents-list" style="max-height: 52vh; overflow-y: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--card);">
                <div class="text-center py-6 text-sm" style="color: var(--text-2)">Loading…</div>
            </div>
        </div>
    </div>

    <!-- Clear logs modal -->
    <div id="clear-modal" class="confirm-modal" onclick="if(event.target===this)closeClearModal()">
        <div class="confirm-box">
            <div class="w-14 h-14 mx-auto mb-4 rounded-full flex items-center justify-center" style="background: rgba(239,68,68,0.1);">
                <i class="fa-solid fa-trash-alt text-2xl" style="color: #ef4444"></i>
            </div>
            <h3 class="font-semibold text-lg mb-2" style="color: var(--text)">Clear All Logs</h3>
            <p class="text-sm mb-5" style="color: var(--text-2)">Enter your key to confirm deletion of all logs.</p>
            <input type="password" id="clear-key-input" placeholder="Enter key" class="input-glass mb-4 text-center" style="letter-spacing: 2px;">
            <p id="clear-error" class="text-sm mb-4 hidden" style="color: #ef4444"></p>
            <div class="flex gap-3">
                <button onclick="closeClearModal()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium" style="background: var(--card); border: 1px solid var(--border); color: var(--text-2)">
                    Cancel
                </button>
                <button onclick="executeClearLogs()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium text-white" style="background: linear-gradient(135deg, #ef4444, #dc2626);">
                    Clear Logs
                </button>
            </div>
        </div>
    </div>
    
    <script>
        const PRODUCTION_LOCK_HOUR = {{ production_lock_hour }};
        
        // Store state_since timestamps
        const stateSince = {};
        
        function isProductionHours() {
            const hour = new Date().getHours();
            return hour < PRODUCTION_LOCK_HOUR;
        }
        
        function updateLockIndicator() {
            const indicator = document.getElementById('lock-indicator');
            if (isProductionHours()) {
                indicator.style.display = 'inline-flex';
            } else {
                indicator.style.display = 'none';
            }
        }
        
        function toggleTheme() {
            document.documentElement.classList.toggle('dark');
            localStorage.theme = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
        }
        
        function toggleDay(el) {
            el.classList.toggle('selected');
        }
        
        function getSelectedDays() {
            const days = [];
            document.querySelectorAll('.day-checkbox.selected').forEach(el => {
                days.push(parseInt(el.dataset.day));
            });
            return days;
        }
        
        function clearDays() {
            document.querySelectorAll('.day-checkbox').forEach(el => el.classList.remove('selected'));
        }
        
        function toggleDropdown(stationId) {
            const card = document.querySelector(`.station-card[data-station-id="${stationId}"]`);
            if (!card) return;
            
            document.querySelectorAll('.station-card.open').forEach(c => {
                if (c !== card) {
                    c.classList.remove('open');
                    const arrow = c.querySelector('.dropdown-arrow');
                    if (arrow) arrow.style.transform = 'rotate(0deg)';
                }
            });
            
            card.classList.toggle('open');
            const arrow = card.querySelector('.dropdown-arrow');
            if (arrow) {
                arrow.style.transform = card.classList.contains('open') ? 'rotate(180deg)' : 'rotate(0deg)';
            }
        }
        
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.station-card') && !e.target.closest('.dropdown-toggle')) {
                document.querySelectorAll('.station-card.open').forEach(c => {
                    c.classList.remove('open');
                    const arrow = c.querySelector('.dropdown-arrow');
                    if (arrow) arrow.style.transform = 'rotate(0deg)';
                });
            }
        });
        
        let userScrolled = false;
        const logsBox = document.getElementById('logs-box');
        logsBox.addEventListener('scroll', () => {
            userScrolled = logsBox.scrollHeight - logsBox.scrollTop > logsBox.clientHeight + 50;
        });
        
        function addLog(msg, level = 'info', time = null) {
            const t = time || new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
            const el = document.createElement('div');
            el.className = 'log-item log-' + level;
            el.innerHTML = '<span style="color:var(--text-3)">' + t + '</span><span style="color:var(--text-2)">' + msg + '</span>';
            logsBox.appendChild(el);
            if (!userScrolled) logsBox.scrollTop = logsBox.scrollHeight;
        }
        
        function updateLastAction(targetId, message, time) {
            const el = document.getElementById('last-action-' + targetId);
            if (el) {
                el.textContent = time + ' · ' + message;
                el.style.display = 'inline-block';
            }
        }
        
        function formatDuration(isoTimestamp) {
            if (!isoTimestamp) return '';
            const since = new Date(isoTimestamp);
            const now = new Date();
            const diffMs = now - since;
            const diffSec = Math.floor(diffMs / 1000);
            
            if (diffSec < 60) return diffSec + 's';
            const diffMin = Math.floor(diffSec / 60);
            if (diffMin < 60) return diffMin + 'm' + (diffSec % 60) + 's';
            const diffHour = Math.floor(diffMin / 60);
            if (diffHour < 24) return diffHour + 'h' + (diffMin % 60) + 'm';
            const diffDays = Math.floor(diffHour / 24);
            return diffDays + 'd' + (diffHour % 24) + 'h';
        }
        
        function updateState(targetId, state, since = null) {
            const dot = document.getElementById('status-' + targetId) || document.getElementById('device-status-' + targetId);
            const badge = document.getElementById('state-badge-' + targetId);
            const timeEl = document.getElementById('state-time-' + targetId);
            
            const stateLower = state.toLowerCase();
            
            // Use new status-dot and status-badge classes
            if (dot) {
                dot.className = 'status-dot status-' + stateLower.replace('_', '-');
            }
            if (badge) {
                badge.className = 'status-badge status-' + stateLower.replace('_', '-');
                const label = badge.querySelector('.state-label');
                if (label) {
                    const stateLabels = {
                        'running': 'Running',
                        'offline': 'Offline',
                        'booting': 'Booting',
                        'shutting_down': 'Shutting Down',
                        'unknown': 'Unknown',
                        'partial': 'Partial'
                    };
                    label.textContent = stateLabels[stateLower] || state;
                }
            }
            
            if (since) {
                stateSince[targetId] = since;
            }
        }
        
        function updateStateTimes() {
            for (const [targetId, since] of Object.entries(stateSince)) {
                const timeEl = document.getElementById('state-time-' + targetId);
                if (timeEl && since) {
                    timeEl.textContent = formatDuration(since);
                }
            }
}
        
        async function fetchLogs() {
            try {
                const r = await fetch('/api/logs');
                const logs = await r.json();
                logsBox.innerHTML = '';
                logs.forEach(l => addLog(l.message, l.level, l.time.slice(0,5)));
                if (logs.length === 0) addLog('System started', 'info');
            } catch (e) {}
        }
        
        async function fetchLastActions() {
            try {
                const r = await fetch('/api/last-actions');
                const data = await r.json();
                for (const [key, val] of Object.entries(data)) {
                    updateLastAction(key, val.message.split(':').pop().trim(), val.time);
                }
            } catch (e) {}
        }
        
        async function wakeup(mac, ip, name, targetId) {
            addLog('WoL → ' + name, 'info');
            updateState(targetId, 'BOOTING', new Date().toISOString());
            try {
                const r = await fetch('/api/wake', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ mac, ip, name, targetId }) 
                });
                const j = await r.json();
                addLog(name + ': ' + j.message, j.status === 'error' ? 'error' : 'success');
                if (targetId) {
                    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
                    updateLastAction(targetId, 'Wake', time);
                }
            } catch (e) { addLog('Error: ' + e.message, 'error'); }
        }
        
        async function wakeStation(stationId, stationName) {
            addLog('Wake station → ' + stationName, 'info');
            updateState(String(stationId), 'BOOTING', new Date().toISOString());
            try {
                const r = await fetch('/api/wake-station', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ stationId: stationId, station_id: stationId }) 
                });
                const j = await r.json();
                addLog(stationName + ': ' + (j.message || j.status), j.status === 'error' ? 'error' : 'success');
                if (stationId) {
                    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
                    updateLastAction(String(stationId), 'Wake all', time);
                }
                checkStatus();
            } catch (e) { addLog('Error: ' + e.message, 'error'); }
        }
        
        let pendingAction = { type: null, ip: null, name: null, targetId: null, stationId: null, isAll: false };
        
        function confirmShutdown(ip, name, targetId, stationId = null) {
            pendingAction = { type: 'shutdown', ip, name, targetId, stationId, isAll: false };
            const displayName = stationId ? name + ' (+ all devices)' : name;
            setupConfirmModal('shutdown', displayName, false);
            document.getElementById('confirm-modal').classList.add('open');
        }
        
        function confirmShutdownAll() {
            pendingAction = { type: 'shutdown', ip: null, name: 'ALL stations', targetId: null, isAll: true };
            setupConfirmModal('shutdown', 'ALL stations', true);
            document.getElementById('confirm-modal').classList.add('open');
        }
        
        function confirmWakeAll() {
            pendingAction = { type: 'wake', ip: null, name: 'ALL stations', targetId: null, isAll: true };
            setupConfirmModal('wake', 'ALL stations', false);
            document.getElementById('confirm-modal').classList.add('open');
        }
        
        function setupConfirmModal(actionType, targetName, requireDoubleConfirm) {
            const iconContainer = document.getElementById('confirm-icon-container');
            const icon = document.getElementById('confirm-icon');
            const title = document.getElementById('confirm-title');
            const actionText = document.getElementById('confirm-action-text');
            const target = document.getElementById('confirm-target');
            const actionBtn = document.getElementById('confirm-action-btn');
            const doubleConfirm = document.getElementById('double-confirm-container');
            const doubleCheck = document.getElementById('double-confirm-check');
            const warningText = document.getElementById('confirm-warning-text');
            const warningDesc = document.getElementById('confirm-warning-desc');
            const bulkResults = document.getElementById('bulk-results');
            
            bulkResults.style.display = 'none';
            bulkResults.innerHTML = '';
            
            target.textContent = targetName;
            
            if (actionType === 'wake') {
                iconContainer.style.background = 'rgba(34,197,94,0.1)';
                icon.className = 'fa-solid fa-bolt text-xl text-green-500';
                title.textContent = 'Confirm Wake';
                actionText.textContent = 'Wake';
                actionBtn.textContent = 'Wake';
                actionBtn.style.background = '#22c55e';
                doubleConfirm.style.display = 'none';
            } else {
                iconContainer.style.background = 'rgba(239,68,68,0.1)';
                icon.className = 'fa-solid fa-power-off text-xl text-red-500';
                title.textContent = 'Confirm Shutdown';
                actionText.textContent = 'Shutdown';
                actionBtn.textContent = 'Shutdown';
                actionBtn.style.background = '#ef4444';
                
                if (requireDoubleConfirm || isProductionHours()) {
                    doubleConfirm.style.display = 'block';
                    doubleCheck.checked = false;
                    
                    if (requireDoubleConfirm && isProductionHours()) {
                        warningText.textContent = 'Shutdown All + Production Mode';
                        warningDesc.textContent = 'I confirm shutting down ALL stations during production hours';
                    } else if (requireDoubleConfirm) {
                        warningText.textContent = 'Shutdown All Stations';
                        warningDesc.textContent = 'I confirm I want to shutdown ALL stations';
                    } else {
                        warningText.textContent = 'Production Mode Active';
                        warningDesc.textContent = 'I confirm this shutdown during production hours';
                    }
                } else {
                    doubleConfirm.style.display = 'none';
                }
            }
        }
        
        function closeConfirm() {
            document.getElementById('confirm-modal').classList.remove('open');
        }
        
        async function executeConfirmedAction() {
            const doubleConfirm = document.getElementById('double-confirm-container');
            const doubleCheck = document.getElementById('double-confirm-check');
            
            if (doubleConfirm.style.display !== 'none' && !doubleCheck.checked) {
                alert('Please confirm the checkbox to proceed');
                return;
            }
            
            const { type, ip, name, targetId, stationId, isAll } = pendingAction;
            
            if (type === 'wake') {
                if (isAll) {
                    await wakeAllWithFeedback();
                }
            } else if (type === 'shutdown') {
                if (isAll) {
                    await shutdownAllWithFeedback();
                } else {
                    closeConfirm();
                    await shutdown(ip, name, targetId, stationId);
                }
            }
            
            pendingAction = { type: null, ip: null, name: null, targetId: null, stationId: null, isAll: false };
        }
        
        async function wakeAllWithFeedback() {
            const bulkResults = document.getElementById('bulk-results');
            const actionBtn = document.getElementById('confirm-action-btn');
            const buttons = document.getElementById('confirm-buttons');
            
            actionBtn.disabled = true;
            actionBtn.textContent = 'Waking...';
            bulkResults.style.display = 'block';
            bulkResults.innerHTML = '<p class="text-sm" style="color: var(--text-3)">Starting wake sequence...</p>';
            
            addLog('Wake All → Starting...', 'info');
            
            try {
                const r = await fetch('/api/wake-all-detailed', { method: 'POST' });
                const j = await r.json();
                
                bulkResults.innerHTML = j.results.map(res => `
                    <div class="bulk-result">
                        <span style="color: var(--text)">${res.name}</span>
                        <span class="${res.status === 'success' ? 'bulk-ok' : 'bulk-fail'}">
                            <i class="fa-solid fa-${res.status === 'success' ? 'check' : 'xmark'}"></i>
                            ${res.status === 'success' ? 'OK' : 'Failed'}
                        </span>
                    </div>
                `).join('');
                
                addLog(`Wake All: ${j.success}/${j.total} sent`, 'success');
                
            } catch (e) {
                bulkResults.innerHTML = '<p class="text-sm text-red-500">Error executing wake</p>';
                addLog('Wake All: Error', 'error');
            }
            
            actionBtn.textContent = 'Done';
            actionBtn.onclick = closeConfirm;
            buttons.innerHTML = `
                <button onclick="closeConfirm()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium text-white" style="background: linear-gradient(135deg, #6366f1, #8b5cf6);">
                    Close
                </button>
            `;
        }
        
        async function shutdownAllWithFeedback() {
            const bulkResults = document.getElementById('bulk-results');
            const actionBtn = document.getElementById('confirm-action-btn');
            const buttons = document.getElementById('confirm-buttons');
            
            actionBtn.disabled = true;
            actionBtn.textContent = 'Shutting down...';
            bulkResults.style.display = 'block';
            bulkResults.innerHTML = '<p class="text-sm" style="color: var(--text-3)">Starting shutdown sequence...</p>';
            
            addLog('Shutdown All → Starting...', 'info');
            
            try {
                const r = await fetch('/api/shutdown-all-detailed', { method: 'POST' });
                const j = await r.json();
                
                bulkResults.innerHTML = j.results.map(res => `
                    <div class="bulk-result">
                        <span style="color: var(--text)">${res.name}</span>
                        <span class="${res.status === 'success' || res.status === 'warning' ? 'bulk-ok' : 'bulk-fail'}">
                            <i class="fa-solid fa-${res.status === 'success' || res.status === 'warning' ? 'check' : 'xmark'}"></i>
                            ${res.status === 'error' ? 'Failed' : 'Sent'}
                        </span>
                    </div>
                `).join('');
                
                addLog(`Shutdown All: ${j.success}/${j.total} sent`, 'warning');
                
            } catch (e) {
                bulkResults.innerHTML = '<p class="text-sm text-red-500">Error executing shutdown</p>';
                addLog('Shutdown All: Error', 'error');
            }
            
            actionBtn.textContent = 'Done';
            buttons.innerHTML = `
                <button onclick="closeConfirm()" class="flex-1 px-4 py-3 rounded-xl text-sm font-medium text-white" style="background: linear-gradient(135deg, #6366f1, #8b5cf6);">
                    Close
                </button>
            `;
        }
        
        async function shutdown(ip, name, targetId, stationId = null) {
            const displayName = name || 'Unknown';
            addLog('Shutdown → ' + displayName + (stationId ? ' (+ devices)' : ''), 'info');
            updateState(targetId, 'SHUTTING_DOWN', new Date().toISOString());
            
            try {
                const body = { ip, name: displayName, targetId };
                if (stationId) {
                    body.stationId = stationId;
                }
                
                const r = await fetch('/api/shutdown', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const j = await r.json();
                
                if (stationId && j.results) {
                    j.results.forEach(res => {
                        addLog(displayName + ' > ' + res.name + ': ' + (res.status === 'error' ? 'Failed' : 'Sent'), 
                            res.status === 'error' ? 'error' : 'warning');
                    });
                    addLog(displayName + ': ' + j.message, j.status === 'error' ? 'error' : 'warning');
                } else {
                    addLog(displayName + ': Shutdown sent', j.status === 'error' ? 'error' : 'warning');
                }
                
                if (targetId) {
                    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
                    updateLastAction(targetId, 'Off', time);
                }
            } catch (e) { addLog('Shutdown error: ' + e.message, 'error'); }
        }
        
        async function pingHost(ip, name, targetId) {
            const displayName = name || 'Unknown';
            addLog('Ping → ' + displayName, 'info');
            try {
                const r = await fetch('/api/ping', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ ip, name: displayName, targetId }) 
                });
                const j = await r.json();
                addLog(displayName + ': ' + (j.online ? 'Online' : 'Offline'), j.online ? 'success' : 'warning');
                updateState(targetId, j.online ? 'RUNNING' : 'OFFLINE', new Date().toISOString());
                if (targetId) {
                    const time = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
                    updateLastAction(targetId, j.online ? 'Online' : 'Offline', time);
                }
            } catch (e) { addLog('Error: ' + e.message, 'error'); }
        }
        
        let previousStatus = {};
        
        async function checkStatus() {
            try {
                const r = await fetch('/api/status');
                const data = await r.json();
                
                data.stations.forEach(s => {
                    // Update state for stations
                    const state = s.state || (s.online ? 'RUNNING' : 'OFFLINE');
                    updateState(s.id, state, s.state_since);
                    
                    if (s.devices) {
                        s.devices.forEach((d) => {
                            const deviceId = s.id + '-' + d.name;
                            const deviceState = d.state || (d.online ? 'RUNNING' : 'OFFLINE');
                            updateState(deviceId, deviceState, d.state_since);
                        });
                    }
                });
            } catch (e) {}
        }
        
        // ── Agents modal ──────────────────────────────────────────
        const AGENT_TARGET_VERSION = '3.4';

        function openAgentsModal() {
            document.getElementById('agents-modal').classList.add('open');
            refreshAgents();
        }

        function closeAgentsModal() {
            document.getElementById('agents-modal').classList.remove('open');
        }

        function agentRowHtml(stationName, d, isLast) {
            const ok = d.online;
            const v  = d.version || '—';
            const outdated = ok && v !== AGENT_TARGET_VERSION;
            const badgeBg = !ok ? 'rgba(107,114,128,0.15)'
                          : outdated ? 'rgba(245,158,11,0.18)'
                          : 'rgba(34,197,94,0.18)';
            const badgeFg = !ok ? '#9ca3af'
                          : outdated ? '#f59e0b'
                          : '#22c55e';
            const dotColor = !ok ? '#9ca3af' : outdated ? '#f59e0b' : '#22c55e';
            const label   = !ok ? 'offline' : ('v' + v);
            const btnDisabled = !ok ? 'disabled style="opacity:0.35;cursor:not-allowed;"' : '';
            const border  = isLast ? '' : 'border-bottom: 1px solid var(--border);';
            return `
              <div class="flex items-center justify-between px-4 py-2.5" style="${border}">
                <div class="flex items-center gap-3 min-w-0">
                    <span style="width:8px;height:8px;border-radius:50%;background:${dotColor};flex-shrink:0;"></span>
                    <div class="min-w-0">
                        <div class="text-sm font-medium truncate" style="color: var(--text)">${d.name}</div>
                        <div class="text-xs truncate" style="color: var(--text-3)">${stationName} · ${d.ip}</div>
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0">
                    <span class="px-2 py-0.5 rounded-md text-xs font-semibold tabular-nums"
                          style="background:${badgeBg};color:${badgeFg};min-width:42px;text-align:center;">${label}</span>
                    <button ${btnDisabled} onclick="updateAgent('${d.ip}', this)"
                            class="px-2.5 py-1 rounded-lg text-xs font-medium inline-flex items-center gap-1"
                            style="background: var(--bg); border: 1px solid var(--border); color: var(--text);">
                        <i class="fa-solid fa-arrow-up-from-bracket" style="font-size:10px;"></i> Update
                    </button>
                </div>
              </div>`;
        }

        async function refreshAgents() {
            const box = document.getElementById('agents-list');
            const sum = document.getElementById('agents-summary');
            box.innerHTML = '<div class="text-center py-6 text-sm" style="color: var(--text-2)">Loading…</div>';
            sum.textContent = '—';
            try {
                const r = await fetch('/api/agents/versions');
                const data = await r.json();
                const rows = [];
                Object.values(data).forEach(s => {
                    (s.devices || []).forEach(d => rows.push({stationName: s.name, d}));
                });
                const total    = rows.length;
                const online   = rows.filter(x => x.d.online).length;
                const current  = rows.filter(x => x.d.online && x.d.version === AGENT_TARGET_VERSION).length;
                const outdated = online - current;
                const offline  = total - online;
                sum.innerHTML = `<span style="color:#22c55e">● ${current} up-to-date</span>` +
                                (outdated ? ` · <span style="color:#f59e0b">● ${outdated} outdated</span>` : '') +
                                (offline  ? ` · <span style="color:#9ca3af">● ${offline} offline</span>` : '');
                if (total === 0) {
                    box.innerHTML = '<div class="text-center py-6 text-sm" style="color: var(--text-2)">No agents</div>';
                } else {
                    box.innerHTML = rows.map((x,i) => agentRowHtml(x.stationName, x.d, i === rows.length - 1)).join('');
                }
            } catch (e) {
                box.innerHTML = '<div class="text-center py-6 text-sm" style="color:#ef4444">Failed to load</div>';
            }
        }

        async function updateAgent(ip, btn) {
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }
            addLog(`Agent update → ${ip}`, 'info');
            try {
                const r = await fetch('/api/agents/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ip})
                });
                const j = await r.json();
                if (j.status === 'success') {
                    addLog(`Agent ${ip} updated → v${j.version}`, 'success');
                } else if (j.status === 'unverified') {
                    addLog(`Agent ${ip} update sent (unverified)`, 'warning');
                } else {
                    const msg = j.message || 'error';
                    if (/unknown endpoint/i.test(msg)) {
                        addLog(`Agent ${ip} is too old (no /update). Install v3.2 manually first.`, 'error');
                    } else {
                        addLog(`Agent ${ip} update failed: ${msg}`, 'error');
                    }
                }
            } catch (e) {
                addLog(`Agent ${ip} update error: ${e}`, 'error');
            }
            refreshAgents();
        }

        async function updateAllAgents() {
            if (!confirm('Update all agents from http://10.30.30.119:9876/script ?')) return;
            addLog('Bulk agent update → starting…', 'info');
            try {
                const r = await fetch('/api/agents/update-all', {method: 'POST'});
                const j = await r.json();
                addLog(`Bulk update: ${j.triggered} triggered / ${j.failed} failed of ${j.total}`, 'info');
            } catch (e) {
                addLog('Bulk agent update error: ' + e, 'error');
            }
            setTimeout(refreshAgents, 12000);
        }

        function openModal() {
            document.getElementById('modal').classList.add('open');
            loadTasks();
        }
        
        function closeModal() {
            document.getElementById('modal').classList.remove('open');
            document.getElementById('schedule-form').reset();
            clearDays();
        }
        
        const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        
        async function loadTasks() {
            try {
                const r = await fetch('/api/schedules');
                const tasks = await r.json();
                const list = document.getElementById('tasks-list');
                
                if (tasks.length === 0) {
                    list.innerHTML = '<p class="text-center py-4" style="color: var(--text-3)">No scheduled tasks</p>';
                    return;
                }
                
                list.innerHTML = tasks.map((t, i) => `
                    <div class="task-item">
                        <div>
                            <div class="font-medium text-sm" style="color: var(--text)">${t.action === 'wake' ? 'Wake' : 'Shutdown'} - ${t.target === 'all' ? 'All stations' : t.target}</div>
                            <div class="text-xs mt-1" style="color: var(--text-3)">${t.time} · ${t.days.map(d => dayNames[d]).join(', ')}</div>
                        </div>
                        <button onclick="deleteTask(${i})" class="task-delete">
                            <i class="fa-solid fa-trash-can text-xs"></i>
                        </button>
                    </div>
                `).join('');
            } catch (e) {
                document.getElementById('tasks-list').innerHTML = '<p class="text-center py-4" style="color: var(--text-3)">Error loading tasks</p>';
            }
        }
        
        async function saveSchedule(e) {
            e.preventDefault();
            const form = e.target;
            const days = getSelectedDays();
            
            if (days.length === 0) {
                alert('Select at least one day');
                return;
            }
            
            const data = {
                action: form.action.value,
                target: form.target.value,
                time: form.time.value,
                days: days
            };
            
            try {
                await fetch('/api/schedules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
                form.reset();
                clearDays();
                loadTasks();
                addLog('Task scheduled: ' + data.action + ' at ' + data.time, 'success');
            } catch (e) { addLog('Error scheduling task', 'error'); }
        }
        
        async function deleteTask(index) {
            try {
                await fetch('/api/schedules/' + index, { method: 'DELETE' });
                loadTasks();
                addLog('Task deleted', 'info');
            } catch (e) {}
        }
        
        function showClearModal() {
            document.getElementById('clear-key-input').value = '';
            document.getElementById('clear-error').classList.add('hidden');
            document.getElementById('clear-modal').classList.add('open');
            document.getElementById('clear-key-input').focus();
        }
        
        function closeClearModal() {
            document.getElementById('clear-modal').classList.remove('open');
        }
        
        async function executeClearLogs() {
            const key = document.getElementById('clear-key-input').value;
            const errorEl = document.getElementById('clear-error');
            
            if (!key) {
                errorEl.textContent = 'Please enter the key';
                errorEl.classList.remove('hidden');
                return;
            }
            
            try {
                const r = await fetch('/api/logs/clear', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key })
                });
                
                const data = await r.json();
                
                if (r.ok) {
                    closeClearModal();
                    logsBox.innerHTML = '';
                    addLog('Logs cleared', 'warning');
                } else {
                    errorEl.textContent = data.message || 'Invalid key';
                    errorEl.classList.remove('hidden');
                }
            } catch (e) {
                errorEl.textContent = 'Error connecting to server';
                errorEl.classList.remove('hidden');
            }
        }
        
        function updateServiceIndicators(stationId, services) {
            const map = { green: 'svc-green', yellow: 'svc-yellow', red: 'svc-red', unknown: 'svc-unknown' };
            ['mongodb', 'redis', 'wtvision'].forEach(svc => {
                const el = document.getElementById('svc-' + svc + '-' + stationId);
                if (!el) return;
                el.className = 'svc-ind ' + (map[services[svc]] || 'svc-unknown');
            });
        }

        async function fetchServices() {
            try {
                const r = await fetch('/api/services');
                const data = await r.json();
                for (const [stationId, services] of Object.entries(data)) {
                    updateServiceIndicators(stationId, services);
                }
            } catch (e) {}
        }

        // Initialize
        fetchLogs();
        fetchLastActions();
        checkStatus();
        fetchServices();
        updateLockIndicator();

        setInterval(checkStatus, 15000);
        setInterval(fetchLogs, 30000);
        setInterval(fetchServices, 30000);
        setInterval(updateLockIndicator, 60000);
        setInterval(updateStateTimes, 1000);
    </script>
    
    <!-- Credits (hidden) - WoL Manager by Osmel Enamorado - Copyright 2026 - All Rights Reserved -->
    <meta name="author" content="Osmel Enamorado">
    <meta name="copyright" content="Copyright 2026 Osmel Enamorado. All rights reserved.">
</body>
</html>
'''

# ============================================================================
# AUTH (temporary; replace with enterprise auth when selling)
# ============================================================================

@app.before_request
def require_auth():
    if auth_users is None:
        return None
    if request.method == 'OPTIONS':
        return None
    auth = request.headers.get('Authorization')
    if not auth or not auth.startswith('Basic '):
        return _auth_required_response()
    try:
        decoded = base64.b64decode(auth[6:]).decode('utf-8')
        username, _, password = decoded.partition(':')
        if auth_users.get(username) == password:
            g.current_username = username
            return None
    except Exception:
        pass
    return _auth_required_response()


def _auth_required_response():
    r = jsonify({"error": "Authentication required"})
    r.status_code = 401
    r.headers['WWW-Authenticate'] = 'Basic realm="WoL Manager"'
    return r


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def index():
    stations = load_stations()
    is_admin = (g.get('current_username') == 'Admin') if auth_users else True
    print(f"[DEBUG] Loaded {len(stations)} stations from JSON")
    for s in stations:
        print(f"[DEBUG] Station: {s.get('id')} - {s.get('name')} - IP: {s.get('ip')} - MAC: {s.get('mac')}")
        for d in s.get('devices', []):
            print(f"[DEBUG]   Device: {d.get('name')} - IP: {d.get('ip')} - MAC: {d.get('mac')}")
    return render_template_string(HTML_TEMPLATE, stations=stations, production_lock_hour=PRODUCTION_LOCK_HOUR, is_admin=is_admin)


@app.route('/api/debug')
def api_debug():
    """Debug endpoint to check what's being loaded"""
    stations = load_stations()
    return jsonify({
        "stations_count": len(stations),
        "stations": stations,
        "stations_file": STATIONS_FILE,
        "file_exists": os.path.exists(STATIONS_FILE)
    })


@app.route('/api/wake', methods=['POST'])
def api_wake():
    data = request.get_json()
    mac = data.get('mac', '')
    ip = data.get('ip', '')
    name = data.get('name', mac)
    target_id = data.get('targetId', None)
    
    result = send_magic_packet(mac, ip_address=ip)
    add_log(f"WoL sent to {name} (MAC: {mac}, IP: {ip})", "success" if result["status"] == "success" else "error", target_id)
    
    if target_id and ip:
        validate_wake(ip, target_id, name)
    
    return jsonify(result)


@app.route('/api/wake-station', methods=['POST'])
def api_wake_station():
    """Wake all devices of a station."""
    data = request.get_json()
    station_id = data.get('stationId') or data.get('station_id')
    if station_id is None:
        return jsonify({"status": "error", "message": "stationId required"}), 400
    
    stations = load_stations()
    station = next((s for s in stations if str(s.get('id')) == str(station_id)), None)
    if not station:
        return jsonify({"status": "error", "message": f"Station {station_id} not found"}), 404
    
    mac_list = []
    device_info = []
    for d in station.get('devices', []):
        mac = (d.get('mac') or '').strip()
        if not mac:
            continue
        mac_list.append(mac)
        device_info.append({
            "name": d.get('name', ''),
            "ip": d.get('ip', ''),
            "mac": mac,
            "device_id": f"{station.get('id')}-{d.get('name')}"
        })
    
    results = []
    for info in device_info:
        result = send_magic_packet(info["mac"], ip_address=info["ip"])
        results.append({"name": info["name"], "status": result.get("status", "error")})
        if info["ip"]:
            validate_wake(info["ip"], info["device_id"], info["name"])

    success_count = sum(1 for r in results if r["status"] == "success")
    add_log(f"Wake station {station.get('name')}: {success_count}/{len(results)} sent", "success", str(station_id))
    return jsonify({
        "status": "success" if success_count > 0 else "error",
        "message": f"Wake sent to {success_count}/{len(results)} devices in {station.get('name')}",
        "results": results,
        "total": len(results),
        "success": success_count
    })


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    data = request.get_json()
    ip = data.get('ip', '')
    name = data.get('name', ip)
    target_id = data.get('targetId', None)
    station_id = data.get('stationId', None)  # If provided, shutdown entire station
    
    results = []
    
    # If stationId is provided, shutdown all devices in that station
    if station_id:
        stations = load_stations()
        station = next((s for s in stations if str(s.get('id')) == str(station_id)), None)
        
        if station:
            print(f"[SHUTDOWN] Shutting down station {station_id} and all its devices")
            
            # Shutdown all devices in the station (SLIDE, STAT, etc.)
            for device in station.get('devices', []):
                device_ip = device.get('ip', '')
                device_name = device.get('name', 'Unknown')
                if device_ip:
                    print(f"[SHUTDOWN] -> Device: {device_name} ({device_ip})")
                    result = shutdown_remote(device_ip)
                    results.append({
                        "name": device_name,
                        "ip": device_ip,
                        "status": result.get('status', 'error'),
                        "message": result.get('message', '')
                    })
                    add_log(f"Shutdown {station.get('name')} > {device_name}", 
                           "warning" if result["status"] != "error" else "error",
                           f"{station_id}-{device_name}")
            
            success_count = sum(1 for r in results if r['status'] in ['success', 'warning'])
            total = len(results)
            
            return jsonify({
                "status": "success" if success_count > 0 else "error",
                "message": f"Shutdown sent to {success_count}/{total} devices in {station.get('name')}",
                "results": results
            })
        else:
            return jsonify({"status": "error", "message": f"Station {station_id} not found"})
    
    # Single device shutdown (original behavior)
    result = shutdown_remote(ip)
    add_log(f"Shutdown sent to {name}", "warning" if result["status"] != "error" else "error", target_id)
    
    if target_id and ip:
        validate_shutdown(ip, target_id, name)
    
    return jsonify(result)


@app.route('/api/ping', methods=['POST'])
def api_ping():
    data = request.get_json()
    ip = data.get('ip', '')
    name = data.get('name', ip)
    target_id = data.get('targetId', None)
    
    result = ping_host_sync(ip)
    add_log(f"Ping {name}: {'Online' if result['online'] else 'Offline'}", 
            "success" if result['online'] else "warning", target_id)
    
    return jsonify(result)


@app.route('/api/wake-all', methods=['POST'])
def api_wake_all():
    """Wake all devices via agent bulk."""
    stations = load_stations()
    all_macs = []
    for s in stations:
        for d in s.get('devices', []):
            mac = (d.get('mac') or '').strip()
            if mac:
                all_macs.append(mac)
    
    # Try bulk via agent (ONE request)
    agent_ip = _get_agent_for_wol()
    if agent_ip and all_macs:
        _send_wol_bulk_via_agent(agent_ip, all_macs)
    else:
        for mac in all_macs:
            send_magic_packet(mac)
    
    add_log(f"Wake All: {len(all_macs)} packets sent", "success")
    return jsonify({"status": "success", "message": f"Wake sent to {len(all_macs)} devices"})


@app.route('/api/wake-all-detailed', methods=['POST'])
def api_wake_all_detailed():
    """Wake all devices via agent bulk with details."""
    stations = load_stations()
    all_macs = []
    device_info = []
    
    for s in stations:
        for d in s.get('devices', []):
            mac = (d.get('mac') or '').strip()
            if not mac:
                continue
            all_macs.append(mac)
            device_info.append({
                "name": f"{s.get('name')} - {d.get('name')}",
                "ip": d.get('ip', ''),
                "device_id": f"{s.get('id')}-{d.get('name')}",
                "device_name": d.get('name')
            })
    
    # Send ALL in ONE request via agent
    agent_ip = _get_agent_for_wol()
    if agent_ip and all_macs:
        _send_wol_bulk_via_agent(agent_ip, all_macs)
    else:
        for mac in all_macs:
            send_magic_packet(mac)
    
    results = []
    for info in device_info:
        results.append({"name": info["name"], "status": "success"})
        if info["ip"]:
            validate_wake(info["ip"], info["device_id"], info["device_name"])
    
    success_count = len(results)
    add_log(f"Wake All: {success_count}/{len(results)} sent", "success")
    return jsonify({
        "status": "success",
        "total": len(results),
        "success": success_count,
        "results": results
    })


@app.route('/api/shutdown-all', methods=['POST'])
def api_shutdown_all():
    """Shutdown all devices by station: only devices with IP in each workstation."""
    stations = load_stations()
    count = 0
    for s in stations:
        for d in s.get('devices', []):
            ip = (d.get('ip') or '').strip()
            if ip:
                shutdown_remote(ip)
                count += 1
    
    add_log(f"Shutdown All: {count} commands sent", "warning")
    return jsonify({"status": "success", "message": f"Shutdown sent to {count} devices"})


@app.route('/api/shutdown-all-detailed', methods=['POST'])
def api_shutdown_all_detailed():
    stations = load_stations()
    results = []
    success_count = 0
    
    for s in stations:
        for d in s.get('devices', []):
            ip = (d.get('ip') or '').strip()
            if not ip:
                continue
            result = shutdown_remote(ip)
            status = result.get('status', 'error')
            results.append({
                "name": f"{s.get('name')} - {d.get('name')}",
                "status": status
            })
            if status in ['success', 'warning']:
                success_count += 1
                device_id = f"{s.get('id')}-{d.get('name')}"
                validate_shutdown(ip, device_id, d.get('name'))
    
    add_log(f"Shutdown All: {success_count}/{len(results)} sent", "warning")
    return jsonify({
        "status": "success",
        "total": len(results),
        "success": success_count,
        "results": results
    })


@app.route('/api/status')
def api_status():
    stations = load_stations()
    check_all_status_async()
    
    result = []
    for s in stations:
        state_info = get_station_state(s.get('id'))
        station_data = {
            "id": s.get('id'),
            "name": s.get('name'),
            "online": state_info.get('state') in ['RUNNING', 'PARTIAL'],
            "state": state_info.get('state', 'UNKNOWN'),
            "state_since": state_info.get('state_since'),
            "devices": []
        }
        
        for d in s.get('devices', []):
            device_id = f"{s.get('id')}-{d.get('name')}"
            device_state_info = get_station_state(device_id)
            station_data["devices"].append({
                "name": d.get('name'),
                "online": device_state_info.get('state') == 'RUNNING',
                "state": device_state_info.get('state', 'UNKNOWN'),
                "state_since": device_state_info.get('state_since')
            })
        
        result.append(station_data)
    
    return jsonify({"stations": result})


@app.route('/api/logs')
def api_logs():
  return jsonify(get_logs())


@app.route('/api/logs/clear', methods=['POST'])
def api_clear_logs():
    global activity_logs
    if auth_users is not None and g.get('current_username') != 'Admin':
        return jsonify({"status": "error", "message": "Admin only"}), 403
    data = request.json or {}
    key = data.get('key', '')
    
    if not CLEAR_KEY:
        return jsonify({"status": "error", "message": "Clear key not configured on server"}), 403
    
    if key != CLEAR_KEY:
        return jsonify({"status": "error", "message": "Invalid key"}), 401
    
    with logs_lock:
        activity_logs = []
    
    # Also clear today's log file
    log_file = get_log_file()
    try:
        if os.path.exists(log_file):
            os.remove(log_file)
    except OSError:
        pass
    
    add_log("Logs cleared manually", "warning")
    return jsonify({"status": "success", "message": "Logs cleared"})


@app.route('/api/last-actions')
def api_last_actions():
    return jsonify(last_actions)


@app.route('/api/schedules', methods=['GET'])
def api_get_schedules():
    return jsonify(load_schedules())


@app.route('/api/schedules', methods=['POST'])
def api_add_schedule():
    global scheduled_tasks
    data = request.get_json()
    schedules = load_schedules()
    schedules.append(data)
    save_schedules(schedules)
    setup_scheduler()
    add_log(f"Task scheduled: {data.get('action')} at {data.get('time')}", "success")
    return jsonify({"status": "success"})


@app.route('/api/services')
def api_services():
    """Query service status (MongoDB, Redis, WTVision) on each station's devices."""
    import urllib.request
    stations = load_stations()

    def query_device_services(ip):
        try:
            req = urllib.request.Request(f"http://{ip}:{AGENT_PORT}/services", method='GET')
            with urllib.request.urlopen(req, timeout=1) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception:
            return None

    def aggregate(device_results, svc_name):
        values = [d.get(svc_name) for d in device_results if d and svc_name in d]
        running = sum(1 for v in values if v == 'running')
        total = len(values)
        if total == 0:       return 'unknown'
        if running == total: return 'green'
        if running > 0:      return 'yellow'
        return 'red'

    result = {}
    futures_map = {}
    for s in stations:
        sid = str(s.get('id'))
        futures_map[sid] = [
            executor.submit(query_device_services, d.get('ip'))
            for d in s.get('devices', []) if d.get('ip')
        ]

    for sid, futs in futures_map.items():
        device_results = [f.result() for f in futs]
        result[sid] = {
            'mongodb':  aggregate(device_results, 'mongodb'),
            'redis':    aggregate(device_results, 'redis'),
            'wtvision': aggregate(device_results, 'wtvision'),
        }

    return jsonify(result)


def _get_agent_version(ip: str, timeout: float = 1.5) -> dict:
    """Return {version, uptime_seconds, host} or {error}."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{ip}:{AGENT_PORT}/status", method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return {
                "ok": True,
                "version": data.get("version", "3.0"),   # v3.0 has no version field
                "uptime": data.get("uptime_seconds"),
                "host": data.get("host"),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send_update_to_agent(ip: str, timeout: float = 6) -> dict:
    """Ask an agent to update itself. Agent responds then self-restarts."""
    import urllib.request
    url = f"http://{ip}:{AGENT_PORT}/update"
    data = json.dumps({"key": AGENT_KEY}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={'Content-Type': 'application/json'},
                                     method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if result.get('status') == 'ok':
                return {"status": "triggered", "message": result.get('message', 'Update scheduled')}
            return {"status": "error", "message": result.get('message', 'Unknown agent error')}
    except Exception as e:
        return {"status": "error", "message": f"Agent unreachable: {e}"}


@app.route('/api/agents/versions')
def api_agents_versions():
    """Report the agent version for every device across all stations."""
    stations = load_stations()
    report = {}
    futures = {}
    for s in stations:
        sid = str(s.get('id'))
        report[sid] = {"name": s.get('name'), "devices": []}
        for d in s.get('devices', []):
            ip = d.get('ip')
            if not ip:
                continue
            futures[(sid, ip, d.get('name'))] = executor.submit(_get_agent_version, ip)

    for (sid, ip, name), fut in futures.items():
        info = fut.result()
        report[sid]["devices"].append({
            "name": name,
            "ip": ip,
            "version": info.get("version") if info.get("ok") else None,
            "online": info.get("ok", False),
            "error": info.get("error") if not info.get("ok") else None,
        })
    return jsonify(report)


@app.route('/api/agents/update', methods=['POST'])
def api_update_agent():
    """Trigger an update on a single agent. Body: { "ip": "10.30.30.x" }."""
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    if not ip:
        return jsonify({"status": "error", "message": "Missing ip"}), 400

    add_log(f"Agent update requested → {ip}", "info")
    result = _send_update_to_agent(ip)

    if result.get('status') == 'triggered':
        # Verify: wait for agent to come back with new version
        import time as _t
        previous = _get_agent_version(ip)
        prev_version = previous.get('version') if previous.get('ok') else None

        new_version = None
        for _ in range(30):  # ~30s grace window (agent restart can take 15-20s)
            _t.sleep(1)
            check = _get_agent_version(ip, timeout=1.5)
            if check.get('ok'):
                v = check.get('version')
                if v and v != prev_version:
                    new_version = v
                    break
                if check.get('uptime') is not None and check.get('uptime') < 15:
                    new_version = v
                    break

        if new_version:
            add_log(f"Agent updated at {ip} → v{new_version}", "success")
            return jsonify({"status": "success", "ip": ip, "version": new_version})
        else:
            add_log(f"Agent update at {ip} triggered — check Agents panel to confirm", "warning")
            return jsonify({"status": "unverified", "ip": ip,
                            "message": "Update sent — refresh Agents panel in a few seconds to confirm"})

    add_log(f"Agent update failed at {ip}: {result.get('message')}", "error")
    return jsonify({"status": "error", "ip": ip, "message": result.get('message')}), 500


@app.route('/api/agents/update-all', methods=['POST'])
def api_update_all_agents():
    """Trigger updates on every reachable device agent, in parallel."""
    stations = load_stations()
    targets = []
    for s in stations:
        for d in s.get('devices', []):
            ip = d.get('ip')
            if ip:
                targets.append({"ip": ip, "name": d.get('name')})

    add_log(f"Bulk agent update requested for {len(targets)} devices", "info")

    def do_one(t):
        r = _send_update_to_agent(t['ip'])
        return {"ip": t['ip'], "name": t['name'], **r}

    futs = [executor.submit(do_one, t) for t in targets]
    results = [f.result() for f in futs]

    triggered = sum(1 for r in results if r.get('status') == 'triggered')
    failed    = sum(1 for r in results if r.get('status') == 'error')
    add_log(f"Bulk agent update: {triggered} triggered, {failed} failed", "info")

    return jsonify({
        "total": len(targets),
        "triggered": triggered,
        "failed": failed,
        "results": results,
    })


@app.route('/api/schedules/<int:index>', methods=['DELETE'])
def api_delete_schedule(index):
    schedules = load_schedules()
    if 0 <= index < len(schedules):
        schedules.pop(index)
        save_schedules(schedules)
        setup_scheduler()
        add_log("Task deleted", "info")
    return jsonify({"status": "success"})


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    load_logs_from_file()
    add_log("System started", "info")
    setup_scheduler()
    app.run(host='0.0.0.0', port=5002, debug=False, threaded=True)

    # This block seems to be a duplicate. Removing one.
    # setup_scheduler()
    # app.run(host='0.0.0.0', port=5002, debug=True, threaded=True)
