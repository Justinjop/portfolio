# Register a Windows scheduled task that runs `python digest.py` every N minutes (default 60).
# Usage (PowerShell, in this folder):   .\schedule_windows.ps1            # every 60 minutes
#                                        .\schedule_windows.ps1 -Minutes 60
#                                        .\schedule_windows.ps1 -Remove     # delete the task
param([int]$Minutes = 60, [switch]$Remove)

$name = "news-digest-bot"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed task '$name'."
    return
}
$python = (Get-Command python -ErrorAction Stop).Source
$dir = $PSScriptRoot
$action = New-ScheduledTaskAction -Execute $python -Argument "digest.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "news-digest-bot: AI news digests to Discord/Telegram/LINE" -Force | Out-Null
Write-Output "Task '$name' runs '$python digest.py' in $dir every $Minutes minutes (first run in 1 minute)."
Write-Output "Check it in Task Scheduler, or remove it with: .\schedule_windows.ps1 -Remove"
