@echo off
setlocal
cd /d "%~dp0" || exit /b 1
where node.exe >nul 2>&1
if errorlevel 1 (
  echo Node.js was not found. Install Node.js 22.14 or newer and try again.
  pause
  exit /b 1
)
node.exe "%~dp0scripts\start-frontend.mjs" %*
if errorlevel 1 (
  pause
  exit /b 1
)
