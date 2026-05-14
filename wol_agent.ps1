# ============================================================================
# WoL Agent v3.4
# ============================================================================
# Author: Osmel Enamorado
# Copyright (c) 2026 Osmel Enamorado. All rights reserved.
# ============================================================================
#
# Usage:
#   .\wol_agent.ps1              - Run the agent
#   .\wol_agent.ps1 -Install     - Register as scheduled task (runs at startup, auto-restart)
#   .\wol_agent.ps1 -Uninstall   - Remove scheduled task
# ============================================================================

param([switch]$Install, [switch]$Uninstall)

$PORT        = 9876
$KEY         = "change_me"  # Must match AGENT_KEY in wol_app.py
$VERSION     = "3.4"
$TASK_NAME   = "WoL Agent"
$BROADCASTS  = @("255.255.255.255", "192.168.1.255")  # Adjust to your subnet
$PORTS       = @(9, 7)
$UPDATE_SRC  = "http://192.168.1.100:9876/script"  # Change to your relay machine IP
$INSTALL_DIR = "C:\Users\Public\WoLAgent"

# ── Helpers: cleanup ─────────────────────────────────────────────────────────

function Kill-Port {
    param([int]$PortNumber)
    # Find and kill every process holding the given TCP port
    try {
        $connections = netstat -ano 2>$null |
            Select-String ":$PortNumber\s" |
            ForEach-Object { ($_ -split '\s+')[-1] } |
            Where-Object { $_ -match '^\d+$' } |
            Sort-Object -Unique
        foreach ($pid_ in $connections) {
            if ([int]$pid_ -ne $PID -and [int]$pid_ -ne 4) {
                try { Stop-Process -Id ([int]$pid_) -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
    } catch {}

    # Also release http.sys URL reservation for this port
    try { netsh http delete urlacl url="http://+:$PortNumber/" 2>$null | Out-Null } catch {}
    try { netsh http delete urlacl url="http://*:$PortNumber/" 2>$null | Out-Null } catch {}

    # Delete any http.sys service point registrations
    try {
        $spList = netsh http show servicepoint 2>$null
        if ($spList -match ":$PortNumber") {
            netsh http delete servicepoint 2>$null | Out-Null
        }
    } catch {}
}

function Stop-AgentProcesses {
    # Pass 1 — kill by CommandLine match
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*wol_agent*' } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
    } catch {}

    # Pass 2 — kill by port ownership (catches renamed/moved scripts)
    Kill-Port -PortNumber $PORT
    Start-Sleep -Milliseconds 800

    # Pass 3 — final sweep on CommandLine
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*wol_agent*' } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
    } catch {}

    Start-Sleep -Milliseconds 500
}

function Remove-OldArtifacts {
    param([switch]$Deep)

    # 1. Stop scheduled task if running
    try { schtasks /end /tn $TASK_NAME 2>$null | Out-Null } catch {}
    Start-Sleep -Milliseconds 300

    # 2. Unregister scheduled task (current name + legacy names)
    foreach ($t in @($TASK_NAME, "WoLAgent", "WoL-Agent", "WakeOnLan")) {
        try { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    }

    # 2b. Remove legacy install folders
    foreach ($legacyDir in @("C:\ProgramData\WoLAgent", "C:\ProgramData\WoL-Agent")) {
        if (Test-Path $legacyDir) {
            try { Remove-Item -Path $legacyDir -Recurse -Force -ErrorAction SilentlyContinue } catch {}
        }
    }

    # 3. Nuclear process + port kill (3 passes)
    Stop-AgentProcesses

    # 4. Release http.sys port binding explicitly
    Kill-Port -PortNumber $PORT

    # 5. Remove temp update scripts (all users + system temp)
    foreach ($tempDir in @($env:TEMP, "C:\Windows\Temp", "$env:SystemRoot\Temp")) {
        Get-ChildItem -Path $tempDir -Filter "wol_agent*" -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    # 6. Remove backup files
    if (Test-Path $INSTALL_DIR) {
        Get-ChildItem -Path $INSTALL_DIR -Filter "*.bak" -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    # 7. Remove copies left on any user's Desktop
    Get-ChildItem -Path "C:\Users" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $desktopCopy = Join-Path $_.FullName "Desktop\wol_agent.ps1"
        if (Test-Path $desktopCopy) {
            try { Remove-Item -Path $desktopCopy -Force -ErrorAction SilentlyContinue } catch {}
        }
    }

    if ($Deep) {
        # 8. Remove install dir entirely
        if (Test-Path $INSTALL_DIR) {
            try { Remove-Item -Path $INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue } catch {}
        }
        # 9. Remove firewall rule
        try { netsh advfirewall firewall delete rule name="WoL Agent" 2>$null | Out-Null } catch {}
    }
}

# ── Self-install ─────────────────────────────────────────────────────────────

if ($Install) {
    # 0. Read THIS script's bytes into memory BEFORE any cleanup —
    #    so even if the source file gets deleted during cleanup, we still have it.
    $sourceBytes = $null
    try {
        $sourceBytes = [System.IO.File]::ReadAllBytes($PSCommandPath)
    } catch {
        Write-Host "ERROR: cannot read source script at $PSCommandPath"
        exit 1
    }

    # 1. Clean previous install (kills processes, removes task, wipes desktop copies, etc.)
    Remove-OldArtifacts

    # 2. Re-create install dir + write fresh script there
    if (-not (Test-Path $INSTALL_DIR)) {
        New-Item -ItemType Directory -Path $INSTALL_DIR -Force | Out-Null
    }
    $targetPath = Join-Path $INSTALL_DIR "wol_agent.ps1"
    [System.IO.File]::WriteAllBytes($targetPath, $sourceBytes)

    # Ensure firewall rule exists (idempotent — replace if present)
    try { netsh advfirewall firewall delete rule name="WoL Agent" 2>$null | Out-Null } catch {}
    try {
        netsh advfirewall firewall add rule name="WoL Agent" dir=in action=allow `
            protocol=TCP localport=$PORT 2>$null | Out-Null
    } catch {}

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
                  -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -NonInteractive -File `"$targetPath`""

    $triggers = @(
        $(New-ScheduledTaskTrigger -AtStartup),
        $(New-ScheduledTaskTrigger -AtLogOn)
    )

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit 0 `
        -MultipleInstances IgnoreNew `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $triggers `
        -Settings $settings -RunLevel Highest -User "SYSTEM" -Force | Out-Null

    Write-Host "WoL Agent v$VERSION installed clean at $targetPath. Old artifacts removed."
    exit
}

if ($Uninstall) {
    Remove-OldArtifacts -Deep
    Write-Host "WoL Agent uninstalled. All artifacts removed."
    exit
}

# ── Hide console window ───────────────────────────────────────────────────────

Add-Type -Name Win -Namespace Native -MemberDefinition `
    '[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
     [DllImport("user32.dll")]   public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
[Native.Win]::ShowWindow([Native.Win]::GetConsoleWindow(), 0) | Out-Null

# ── RunspacePool for parallel request handling ────────────────────────────────

$startTime = Get-Date
$pool = [RunspaceFactory]::CreateRunspacePool(1, 10)
$pool.Open()

$handlerScript = {
    param($context, $key, $startTime, $version, $broadcasts, $ports, $updateSrc, $installDir, $taskName)

    $req = $context.Request
    $res = $context.Response

    try {
        $res.Headers.Add("Access-Control-Allow-Origin", "*")
        $res.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        $res.Headers.Add("Access-Control-Allow-Headers", "Content-Type")

        if ($req.HttpMethod -eq "OPTIONS") {
            $res.StatusCode = 200
            $res.Close()
            return
        }

        $json = ""

        switch ($req.Url.LocalPath) {

            "/status" {
                $uptime = [int]((Get-Date) - $startTime).TotalSeconds
                $json = "{`"status`":`"running`",`"host`":`"$env:COMPUTERNAME`",`"version`":`"$version`",`"uptime_seconds`":$uptime}"
            }

            "/wake" {
                if ($req.HttpMethod -ne "POST") {
                    $json = '{"status":"error","message":"Use POST"}'
                    break
                }

                $reader = New-Object System.IO.StreamReader($req.InputStream)
                $body   = $reader.ReadToEnd()
                $reader.Close()

                try {
                    $data = $body | ConvertFrom-Json

                    if ($data.key -ne $key) {
                        $json = '{"status":"error","message":"Invalid key"}'
                        break
                    }

                    $macs = @()
                    if ($data.mac)  { $macs += $data.mac }
                    if ($data.macs) { $macs += $data.macs }

                    $sent = 0
                    foreach ($macAddr in $macs) {
                        $clean = ($macAddr -replace '[^0-9A-Fa-f]', '')
                        if ($clean.Length -ne 12) { continue }

                        $macBytes = [byte[]]::new(6)
                        for ($i = 0; $i -lt 6; $i++) {
                            $macBytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
                        }
                        $magic = [byte[]]::new(102)
                        for ($i = 0; $i -lt 6; $i++) { $magic[$i] = 0xFF }
                        for ($i = 0; $i -lt 16; $i++) {
                            [Array]::Copy($macBytes, 0, $magic, 6 + $i * 6, 6)
                        }

                        # 3 attempts for reliability
                        for ($attempt = 0; $attempt -lt 3; $attempt++) {
                            $udp = New-Object System.Net.Sockets.UdpClient
                            $udp.EnableBroadcast = $true
                            foreach ($bcast in $broadcasts) {
                                foreach ($port in $ports) {
                                    try { $udp.Send($magic, $magic.Length, $bcast, $port) | Out-Null } catch {}
                                }
                            }
                            $udp.Close()
                            if ($attempt -lt 2) { Start-Sleep -Milliseconds 200 }
                        }
                        $sent++
                    }

                    $json = "{`"status`":`"success`",`"sent`":$sent}"
                } catch {
                    $json = '{"status":"error","message":"Parse error"}'
                }
            }

            "/shutdown" {
                if ($req.HttpMethod -ne "POST") {
                    $json = '{"status":"error","message":"Use POST"}'
                    break
                }

                $reader = New-Object System.IO.StreamReader($req.InputStream)
                $body   = $reader.ReadToEnd()
                $reader.Close()

                try {
                    $data = $body | ConvertFrom-Json

                    if ($data.key -ne $key) {
                        $json = '{"status":"error","message":"Invalid key"}'
                        break
                    }

                    # Respond before shutting down
                    $buf = [System.Text.Encoding]::UTF8.GetBytes('{"status":"ok","message":"Shutdown initiated"}')
                    $res.ContentType = "application/json"
                    $res.ContentLength64 = $buf.Length
                    $res.OutputStream.Write($buf, 0, $buf.Length)
                    $res.Close()
                    Start-Process "shutdown.exe" -ArgumentList "/s /f /t 1" -WindowStyle Hidden
                    return
                } catch {
                    $json = '{"status":"error","message":"Invalid JSON"}'
                }
            }

            "/script" {
                # Serve the currently installed .ps1 so other agents can self-update
                $installedPath = Join-Path $installDir "wol_agent.ps1"
                if (-not (Test-Path $installedPath)) {
                    $json = '{"status":"error","message":"Script not found"}'
                    break
                }
                try {
                    $bytes = [System.IO.File]::ReadAllBytes($installedPath)
                    $res.ContentType = "text/plain; charset=utf-8"
                    $res.Headers.Add("X-Agent-Version", $version)
                    $res.ContentLength64 = $bytes.Length
                    $res.OutputStream.Write($bytes, 0, $bytes.Length)
                    $res.Close()
                    return
                } catch {
                    $json = '{"status":"error","message":"Read failed"}'
                }
            }

            "/update" {
                if ($req.HttpMethod -ne "POST") {
                    $json = '{"status":"error","message":"Use POST"}'
                    break
                }

                $reader = New-Object System.IO.StreamReader($req.InputStream)
                $body   = $reader.ReadToEnd()
                $reader.Close()

                try {
                    $data = $body | ConvertFrom-Json
                    if ($data.key -ne $key) {
                        $json = '{"status":"error","message":"Invalid key"}'
                        break
                    }

                    $installedPath = Join-Path $installDir "wol_agent.ps1"
                    $backupPath    = Join-Path $installDir "wol_agent.ps1.bak"

                    # 1. Download new version via HTTP as raw bytes (preserves encoding)
                    $newBytes = $null
                    try {
                        $resp = Invoke-WebRequest -Uri $updateSrc -UseBasicParsing -TimeoutSec 10
                        $newBytes = $resp.RawContentStream.ToArray()
                        if (-not $newBytes -or $newBytes.Length -lt 500) {
                            $json = '{"status":"error","message":"Downloaded script looks invalid"}'
                            break
                        }
                    } catch {
                        $errMsg = $_.Exception.Message -replace '"',"'"
                        $json = "{`"status`":`"error`",`"message`":`"Download failed: $errMsg`"}"
                        break
                    }

                    # 2. Backup current
                    if (Test-Path $installedPath) {
                        Copy-Item -Path $installedPath -Destination $backupPath -Force -ErrorAction SilentlyContinue
                    }

                    # 3. Write new version (raw bytes, no re-encoding)
                    [System.IO.File]::WriteAllBytes($installedPath, $newBytes)

                    # 4. Respond BEFORE self-destruct
                    $respBuf = [System.Text.Encoding]::UTF8.GetBytes('{"status":"ok","message":"Update scheduled"}')
                    $res.ContentType = "application/json"
                    $res.ContentLength64 = $respBuf.Length
                    $res.OutputStream.Write($respBuf, 0, $respBuf.Length)
                    $res.Close()

                    # 5. Detached relaunch: kill old, run -Install (which cleans + reinstalls), start task, self-delete
                    $relaunch = @"
Start-Sleep -Seconds 2
try { schtasks /end /tn "$taskName" | Out-Null } catch {}
Start-Sleep -Seconds 1
try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { `$_.CommandLine -like '*wol_agent.ps1*' } |
        ForEach-Object { try { Stop-Process -Id `$_.ProcessId -Force } catch {} }
} catch {}
Start-Sleep -Seconds 1
powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -NonInteractive -File "$installedPath" -Install | Out-Null
Start-Sleep -Seconds 1
schtasks /run /tn "$taskName" | Out-Null
Start-Sleep -Seconds 2
try { Remove-Item -LiteralPath `$MyInvocation.MyCommand.Path -Force } catch {}
"@
                    $tmpScript = Join-Path $env:TEMP "wol_agent_update.ps1"
                    Set-Content -Path $tmpScript -Value $relaunch -Encoding UTF8
                    Start-Process -FilePath "powershell.exe" `
                        -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -NonInteractive -File `"$tmpScript`"" `
                        -WindowStyle Hidden
                    return
                } catch {
                    $json = "{`"status`":`"error`",`"message`":`"Update failed: $($_.Exception.Message -replace '\"','')`"}"
                }
            }

            "/services" {
                function Get-SvcStatus($pattern) {
                    $svc = Get-Service | Where-Object { $_.DisplayName -like $pattern } | Select-Object -First 1
                    if ($svc) { return $svc.Status.ToString().ToLower() } else { return "not_found" }
                }
                $mongo   = Get-SvcStatus "*MongoDB*"
                $redis   = Get-SvcStatus "*Redis*"
                $wtvision = Get-SvcStatus "*wTVision*"
                $json = "{`"mongodb`":`"$mongo`",`"redis`":`"$redis`",`"wtvision`":`"$wtvision`"}"
            }

            default {
                $json = '{"status":"error","message":"Unknown endpoint"}'
            }
        }

        $buf = [System.Text.Encoding]::UTF8.GetBytes($json)
        $res.ContentType = "application/json"
        $res.ContentLength64 = $buf.Length
        $res.OutputStream.Write($buf, 0, $buf.Length)
        $res.Close()

    } catch {
        try { $res.Close() } catch {}
    }
}

# ── Main loop with auto-restart ───────────────────────────────────────────────

while ($true) {
    $listener = $null
    try {
        $listener = New-Object System.Net.HttpListener
        $listener.Prefixes.Add("http://+:$PORT/")
        $listener.Start()

        while ($true) {
            $context = $listener.GetContext()

            $ps = [PowerShell]::Create()
            $ps.RunspacePool = $pool
            $ps.AddScript($handlerScript)      | Out-Null
            $ps.AddParameter('context',    $context)    | Out-Null
            $ps.AddParameter('key',        $KEY)        | Out-Null
            $ps.AddParameter('startTime',  $startTime)  | Out-Null
            $ps.AddParameter('version',    $VERSION)    | Out-Null
            $ps.AddParameter('broadcasts', $BROADCASTS) | Out-Null
            $ps.AddParameter('ports',      $PORTS)      | Out-Null
            $ps.AddParameter('updateSrc',  $UPDATE_SRC) | Out-Null
            $ps.AddParameter('installDir', $INSTALL_DIR)| Out-Null
            $ps.AddParameter('taskName',   $TASK_NAME)  | Out-Null
            $ps.BeginInvoke() | Out-Null
        }
    } catch {
        try { $listener.Stop(); $listener.Close() } catch {}
        Start-Sleep -Seconds 3
    }
}
