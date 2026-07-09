@echo off
rem Build standalone fuel-map-server.exe (index.html embedded)
cd /d "%~dp0"

python -m pip install --quiet --disable-pip-version-check pyinstaller
if errorlevel 1 goto err

rem stop running instances so dist\fuel-map-server.exe can be overwritten
taskkill /f /im fuel-map-server.exe >nul 2>&1

python -m PyInstaller --onefile --name fuel-map-server --add-data "index.html;." server.py --log-level WARN
if errorlevel 1 goto err

echo.
echo OK: dist\fuel-map-server.exe
exit /b 0

:err
echo.
echo BUILD FAILED
exit /b 1
