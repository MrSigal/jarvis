@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Installing J.A.R.V.I.S
echo ==========================================
echo       Installing J.A.R.V.I.S  - please wait
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.12 first - tick "Add python.exe to PATH".
  start https://www.python.org/downloads/release/python-31210/
  pause
  exit /b
)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo *** Installation failed. Take a screenshot of this window and send it. ***
  pause
  exit /b
)
echo.
echo Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\JARVIS.lnk\"); $s.TargetPath='%~dp0run_jarvis.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.IconLocation='%SystemRoot%\System32\shell32.dll,43'; $s.Save()"
echo.
echo ==========================================
echo   DONE!  A "JARVIS" icon is now on your desktop.
echo   Double-click it to start.
echo ==========================================
pause
