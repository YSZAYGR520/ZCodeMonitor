@echo off
rem Prefer standalone EXE (no Python needed); auto-fallback to pythonw if
rem the EXE is blocked by security policy or missing. No parentheses in
rem echo text inside if-blocks - they would break batch parsing.
cd /d "%~dp0"
if exist "ZCodeMonitor.exe" (
  start "" "ZCodeMonitor.exe"
  timeout /t 2 /nobreak >nul
  tasklist /FI "IMAGENAME eq ZCodeMonitor.exe" 2>nul | find /I "ZCodeMonitor.exe" >nul
  if not errorlevel 1 exit /b 0
  echo [WARN] ZCodeMonitor.exe did not start - blocked by App Control? Fallback to pythonw...
)
where pythonw >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot run: ZCodeMonitor.exe blocked and pythonw not found.
  echo Install Python 3.8+ then run: python zcode_monitor_tk.py
  pause
  exit /b 1
)
start "" pythonw "zcode_monitor_tk.py"
