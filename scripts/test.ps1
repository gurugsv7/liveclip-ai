<#  Runs all automated tests. -Fast skips the ~4 min media integration suite. #>
param([switch]$Fast)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Root "backend\.venv\Scripts\python.exe"
Push-Location (Join-Path $Root "backend")
if ($Fast) { & $vpy -m pytest tests/test_units.py -v } else { & $vpy -m pytest -v }
$be = $LASTEXITCODE
Pop-Location
Push-Location (Join-Path $Root "frontend")
npx vitest run
$fe = $LASTEXITCODE
Pop-Location
if ($be -ne 0 -or $fe -ne 0) { exit 1 }
