@echo off
rem Double-click: stops LiveClip AI cleanly, then shuts the laptop down in 30 seconds.
rem Cancel within 30 s with:  shutdown /a
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\shutdown-30s.ps1"
pause
