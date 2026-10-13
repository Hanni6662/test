@echo off
setlocal
cd /d "%~dp0" || exit /b 1
where node.exe >nul 2>&1
if errorlevel 1 (
  echo Node.js 22.14 or newer is required. Install it and try again.
  pause
  exit /b 1
)
node.exe "%~dp0scripts\start-workbench.mjs" %*
if errorlevel 1 (
  echo.
  echo Startup failed. See the messages above.
  pause
  exit /b 1
)
