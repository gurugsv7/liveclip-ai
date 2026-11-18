<#  Dev mode: backend with auto-reload on :8765 + Vite dev server on :5173 (proxying /api, /media, /ws) #>
$Root = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Root "backend\.venv\Scripts\python.exe"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\backend'; & '$vpy' -m liveclip --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\frontend'; npm run dev"
Start-Sleep 4
Start-Process "http://127.0.0.1:5173/"
