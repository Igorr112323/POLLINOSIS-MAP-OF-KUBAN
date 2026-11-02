@echo off
setlocal
cd /d "%~dp0.."
where npm >nul 2>nul || (echo Node.js 22.12+ and npm are required. & exit /b 1)
if not exist .venv\Scripts\python.exe (
  where py >nul 2>nul || (echo Python 3.11+ with py launcher is required. & exit /b 1)
  py -3.12 -m venv .venv
  if errorlevel 1 py -3.11 -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r backend\requirements.txt
if errorlevel 1 exit /b 1
pushd frontend
if not exist node_modules\.pollinoz-lock goto install_frontend
fc /b package-lock.json node_modules\.pollinoz-lock >nul
if not errorlevel 1 goto build_frontend
:install_frontend
call npm ci --prefer-offline --no-audit --no-fund
if errorlevel 1 (popd & exit /b 1)
copy /y package-lock.json node_modules\.pollinoz-lock >nul
:build_frontend
call npm run build
if errorlevel 1 (popd & exit /b 1)
popd
if not defined DEMO_MODE set DEMO_MODE=true
if not defined OFFLINE set OFFLINE=false
start "Pollinoz Backend - close this window to stop" /D "%CD%\backend" cmd /k "..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001"
pushd frontend
call npm run dev -- --host 127.0.0.1
popd
echo Close the Backend window to stop the API.
endlocal
