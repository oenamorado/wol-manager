@echo off
:: ============================================================================
:: WoL Manager - Restart Service
:: Author: Osmel Enamorado
:: Copyright (c) 2026 Osmel Enamorado. All rights reserved.
:: ============================================================================
title WoL Manager - Restarting...
cd /d "%~dp0"

echo  [1/3] Stopping WoL Manager...
powershell -ExecutionPolicy Bypass -Command ^
    "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*wol_app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo  [2/3] Starting WoL Manager...
powershell -WindowStyle Hidden -ExecutionPolicy Bypass -Command ^
    "Start-Process python -ArgumentList '%~dp0wol_app.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
timeout /t 2 /nobreak >nul

echo  [3/3] Done! Closing in 3 seconds...
timeout /t 3 /nobreak >nul
exit
