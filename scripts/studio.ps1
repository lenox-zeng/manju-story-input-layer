# Start the v2.0 Studio on port 8092 with MANJU_ROOT pinned to this project.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:MANJU_ROOT = $root
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python -m manju.studio --host 127.0.0.1 --port 8092 @args
exit $LASTEXITCODE
