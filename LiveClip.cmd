@echo off
rem Windows: double-click to start LiveClip AI and open the dashboard (opens it directly if already running).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 pause
