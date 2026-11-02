# Run the v2.0 story CLI with MANJU_ROOT pinned to this project.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:MANJU_ROOT = $root
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python -m manju.story_cli @args
exit $LASTEXITCODE
