param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string]$Nssm = 'nssm.exe',
    [string]$ServiceName = 'ProxyHub'
)
$ErrorActionPreference = 'Stop'
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ProjectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Run run.bat first to install dependencies.' }
# Run this script explicitly in an Administrator terminal to install the service.
& $Nssm install $ServiceName $PythonPath '-m proxyhub'
if ($LASTEXITCODE -ne 0) { throw 'NSSM install failed' }
& $Nssm set $ServiceName AppDirectory $ProjectPath
& $Nssm set $ServiceName AppStopMethodConsole 15000
& $Nssm set $ServiceName AppKillProcessTree 1
& $Nssm set $ServiceName AppNoConsole 1
& $Nssm set $ServiceName Start SERVICE_AUTO_START
Write-Host "Installed $ServiceName. Start it with: nssm start $ServiceName"
