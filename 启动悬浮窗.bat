@echo off
rem Prefer standalone EXE (no Python needed); fall back to system pythonw (tkinter build)
cd /d "%~dp0"
if exist "ZCodeMonitor.exe" (
  start "" "ZCodeMonitor.exe"
) else (
  where pythonw >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] ZCodeMonitor.exe not found and pythonw not in PATH.
    pause
    exit /b 1
  )
  start "" pythonw "zcode_monitor_tk.py"
)
