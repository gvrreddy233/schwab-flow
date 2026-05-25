# Registers the one-time SchwabFlow-Verify task to run whether the user is
# logged on or not (S4U). Requires an elevated (administrator) PowerShell.
#   powershell -ExecutionPolicy Bypass -File .\register_verify.ps1
$ErrorActionPreference = 'Stop'

$name       = 'SchwabFlow-Verify'
$batch      = 'C:\Users\gvrre\schwab-flow\run_verify.bat'
$projectDir = 'C:\Users\gvrre\schwab-flow'
$runAt      = Get-Date '2026-05-26 16:45:00'

try { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop } catch {}

$action    = New-ScheduledTaskAction -Execute $batch -WorkingDirectory $projectDir
$trigger   = New-ScheduledTaskTrigger -Once -At $runAt
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                 -DontStopIfGoingOnBatteries -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
# S4U = run whether the user is logged on or not (no stored password needed).
$principal = New-ScheduledTaskPrincipal -UserId 'LAPTOP-7HL66GEK\gvrre' -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'One-time: verify 2026-05-26 captures (runs whether logged in or not)' | Out-Null

Write-Host "Registered $name (S4U) for $runAt"
