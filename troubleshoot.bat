@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JARVIS - troubleshooting
python jarvis.py
echo.
echo If you see an error above, take a screenshot and send it.
pause
