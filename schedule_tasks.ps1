# Registers three Windows Scheduled Tasks that run run_capture.bat weekdays:
#   SchwabFlow-PreMarket  09:00 ET  (pre-market snapshot)
#   SchwabFlow-Intraday   12:30 ET  (mid-session)
#   SchwabFlow-PostClose  16:30 ET  (after market close)
#
# Idempotent: removes any task with the same name before recreating.
# Run from an elevated PowerShell prompt:
#     powershell -ExecutionPolicy Bypass -File .\schedule_tasks.ps1

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Batch      = Join-Path $ProjectDir 'run_capture.bat'

if (-not (Test-Path $Batch)) {
    throw "run_capture.bat not found at $Batch"
}

$Schedules = @(
    @{ Name = 'SchwabFlow-PreMarket';  Time = '09:00' },
    @{ Name = 'SchwabFlow-Intraday';   Time = '12:30' },
    @{ Name = 'SchwabFlow-PostClose';  Time = '16:30' }
)

foreach ($s in $Schedules) {
    $name = $s.Name
    $time = $s.Time

    # Remove existing task with the same name (idempotent).
    try { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop } catch {}

    $action  = New-ScheduledTaskAction -Execute $Batch -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -Weekly `
                  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                  -At $time
    $settings = New-ScheduledTaskSettingsSet `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -StartWhenAvailable `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    # Run whether user is logged in or not, using current user identity.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

    Register-ScheduledTask -TaskName $name `
                           -Action $action `
                           -Trigger $trigger `
                           -Settings $settings `
                           -Principal $principal `
                           -Description "Schwab option chain capture ($time ET weekdays)" | Out-Null

    Write-Host "Registered: $name  at $time  -> $Batch"
}

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "    Get-ScheduledTask -TaskName 'SchwabFlow-*'"
Write-Host ""
Write-Host "To trigger one immediately for testing:"
Write-Host "    Start-ScheduledTask -TaskName SchwabFlow-Intraday"
