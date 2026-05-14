@echo off
:: ============================================================================
:: WoL Manager - Start (silent)
:: Author: Osmel Enamorado
:: Copyright (c) 2026 Osmel Enamorado. All rights reserved.
:: ============================================================================
cd /d "%~dp0"
powershell -WindowStyle Hidden -ExecutionPolicy Bypass -Command ^
    "Start-Process python -ArgumentList '%~dp0wol_app.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
exit
