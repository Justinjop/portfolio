# Register a Windows scheduled task that runs `python watch.py check` every N minutes (default 30).
# Usage (PowerShell, in this folder):   .\schedule_windows.ps1            # every 30 minutes
#                                        .\schedule_windows.ps1 -Minutes 60
#                                        .\schedule_windows.ps1 -Remove     # delete the task
param([int]$Minutes = 30, [switch]$Remove)

$name = "price-watch"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed task '$name'."
    return
}
$python = (Get-Command python -ErrorAction Stop).Source
$dir = $PSScriptRoot
$action = New-ScheduledTaskAction -Execute $python -Argument "watch.py check" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "price-watch: price alerts to Discord/Telegram" -Force | Out-Null
Write-Output "Task '$name' runs '$python watch.py check' in $dir every $Minutes minutes (first run in 1 minute)."
Write-Output "Check it in Task Scheduler, or remove it with: .\schedule_windows.ps1 -Remove"
