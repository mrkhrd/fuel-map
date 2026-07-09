@echo off
rem Build compact Rust host (index.html embedded, ~210 KB)
cd /d "%~dp0"

rem stop running instances so the exe can be overwritten
taskkill /f /im fuel-host.exe >nul 2>&1

cargo build --release
if errorlevel 1 goto err

echo.
echo OK: target\release\fuel-host.exe
for %%A in (target\release\fuel-host.exe) do echo size: %%~zA bytes
exit /b 0

:err
echo.
echo BUILD FAILED
exit /b 1
