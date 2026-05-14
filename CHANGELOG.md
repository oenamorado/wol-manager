# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.4] - 2026-05-12

### Added
- Initial public release
- Flask-based web UI for Wake-on-LAN management
- PowerShell agent for Windows machines
- Real-time device status monitoring
- Service health indicators (MongoDB, Redis, custom services)
- Relay agent support for cross-subnet WoL
- Scheduled tasks (APScheduler-based cron automation)
- Production Mode time-lock to prevent accidental shutdowns during broadcast
- Agent auto-update capability
- Dark/Light mode UI preference
- Persistent audit logging
- Optional HTTP basic authentication
- Responsive web interface

### Features
- **Wake on LAN** - Send magic packets to individual devices or entire workstations
- **Remote Shutdown** - Graceful shutdown via lightweight PowerShell agent
- **Real-time Status** - Live ping monitoring for every device
- **Relay Agent** - Overcome subnet limitations
- **Dual-path WoL** - Relay + direct UDP for maximum reliability
- **Scheduled Tasks** - Automated wake/shutdown by day and time
- **Production Mode** - Time-based lock that warns during broadcast hours
- **Agent Auto-update** - Push new agent versions from the UI
- **Dark/Light Mode** - Persistent user preference
- **Persistent Logs** - Full audit trail with timestamps
- **Auth Support** - Optional basic authentication layer

### Platform Support
- **Server:** Python 3.8+, Flask 2.x, APScheduler 3.10+
- **Agents:** Windows 10/11, Server 2016+, PowerShell 5.1+

### Known Limitations
- HTTP/plain text agent communication (intended for isolated private networks only)
- No built-in TLS/HTTPS (add reverse proxy for public exposure)
- Single-server deployment (no clustering)

---

## Notes

This project was created for managing power on multi-workstation broadcast studio environments. It has been designed with simplicity, reliability, and ease of deployment in mind.

For detailed setup and troubleshooting, see [SETUP.md](SETUP.md).
