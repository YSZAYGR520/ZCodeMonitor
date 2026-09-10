@echo off
cd /d "%~dp0"
echo ============================================
echo   ZCode Usage Stats CLI
echo ============================================
echo.
python zcode_usage_cli.py %*
echo.
echo ------------------------------------------------------------
echo  Tips: try these for other time ranges / output modes
echo    python zcode_usage_cli.py --window 7d
echo    python zcode_usage_cli.py --json
echo ------------------------------------------------------------
echo.
pause
