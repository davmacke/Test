$ErrorActionPreference='Stop'
$dataPath=Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
try {
 $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/healthz' -TimeoutSec 2
 if($health.status-eq 'ok'){Write-Output 'Property Scout is already running: http://127.0.0.1:8765';return}
} catch {}
$runtime=Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if(-not(Test-Path -LiteralPath $runtime)){$runtime=(Get-Command python -ErrorAction Stop).Source}
$env:AUTO_SCAN='true';$env:SCAN_INTERVAL_HOURS='12';$env:PAGES_PER_SOURCE='20'
$env:DATA_DIR=$dataPath;$env:BIND_HOST='127.0.0.1';$env:PORT='8765'
$scriptPath=Join-Path $PSScriptRoot 'app.py'
$process=Start-Process -FilePath $runtime -ArgumentList @('-u',('"'+$scriptPath+'"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataPath 'server.log') -RedirectStandardError (Join-Path $dataPath 'server-error.log') -PassThru
Set-Content -LiteralPath (Join-Path $dataPath 'server.pid') -Value $process.Id
Write-Output 'Property Scout started: http://127.0.0.1:8765'
Write-Output 'Checks run every 12 hours while this process and computer remain running. Nothing is installed to Windows startup.'
