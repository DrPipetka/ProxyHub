param([string]$ProjectPath = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ProjectPath '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Run run.bat first.' }
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument '-m proxyhub' -WorkingDirectory $ProjectPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ProxyHub' -Action $Action -Trigger $Trigger -Settings $Settings -Description 'ProxyHub local proxy pool' -Force
