$ErrorActionPreference='Stop'
$pidFile=Join-Path $PSScriptRoot 'data\server.pid'
if(-not(Test-Path -LiteralPath $pidFile)){Write-Output 'No launcher-owned process recorded.';return}
$scoutProcessId=[int](Get-Content -LiteralPath $pidFile)
$process=Get-CimInstance Win32_Process -Filter "ProcessId=$scoutProcessId"
$expectedScript=Join-Path $PSScriptRoot 'app.py'
if($null-eq $process){Write-Output 'Property Scout is already stopped.';return}
if($process.Name-notmatch '^python(?:w)?\.exe$'-or $process.CommandLine-notlike ('*'+$expectedScript+'*')){throw 'PID no longer identifies this Property Scout app; no process stopped.'}
Stop-Process -Id $scoutProcessId
Write-Output 'Property Scout stopped. Saved research is retained.'
