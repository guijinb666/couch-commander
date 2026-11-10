@echo off
setlocal
chcp 65001 >nul 2>&1
title Couch Commander - Start Service

rem ============================================================
rem  Portable one-click launcher (background / daemon mode).
rem  Paths are relative to this .bat, so the whole folder can be
rem  unpacked anywhere (Desktop, D:\, USB drive, ...).
rem
rem  What changed and why:
rem    The old version ran "python main.py" as a FOREGROUND child of
rem    this console. Closing that console window (or logging off, or
rem    the shell being recycled) killed the service instantly, and a
rem    crash in main.py was never restarted - the phone just saw the
rem    connection die with no trace. That is the "background suddenly
rem    disconnected" behaviour.
rem    Now we hand off to service.py, which starts main.py in a
rem    DETACHED_PROCESS (no console at all), supervises it, and
rem    restarts it with backoff if it ever exits non-zero.
rem
rem  Optional: pass "selftest" to print an environment snapshot
rem            WITHOUT starting the service.
rem  This file is ASCII-only on purpose (any cmd codepage is safe).
rem ============================================================

set "ROOT=%~dp0"
set "APP_DIR=%ROOT%server"
set "PORT=8765"
set "FWNAME=CouchCommander8765"

if /I "%~1"=="selftest" goto :selftest

cd /d "%APP_DIR%"
if errorlevel 1 goto :no_dir

echo ============================================================
echo    Couch Commander - portable launcher
echo    Folder: %ROOT%
echo    Mode  : background service (survives closing this window)
echo ============================================================

rem ---- 1) Core dependency check (install only when missing) ----
rem cryptography is checked too: without it the service silently falls back
rem to HTTP and the phone will never be asked for microphone permission.
python -c "import aiohttp, cryptography" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing missing dependencies ...
  python -m pip install -r "%APP_DIR%\requirements.txt"
)

rem ---- 2) Firewall: allow TCP 8765 inbound (one-time) ----
net session >nul 2>&1
if %errorlevel%==0 goto :fw_admin
goto :fw_user

:fw_admin
netsh advfirewall firewall show rule name=%FWNAME% >nul 2>&1
if %errorlevel%==1 (
  echo [INFO] Adding firewall allow rule for TCP %PORT% ...
  netsh advfirewall firewall add rule name=%FWNAME% dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1
)
goto :launch

:fw_user
rem Not admin: try a one-time UAC elevation for the firewall rule only.
rem If the user cancels UAC, the service still starts (local access is
rem unaffected); phone access then needs the netsh command below once.
netsh advfirewall firewall show rule name=%FWNAME% >nul 2>&1
if %errorlevel%==1 (
  echo [INFO] Requesting admin to allow TCP %PORT% for phone access ...
  powershell -NoProfile -Command "Start-Process netsh -ArgumentList 'advfirewall firewall add rule name=%FWNAME% dir=in action=allow protocol=TCP localport=%PORT%' -Verb RunAs" >nul 2>&1
)
echo [NOTE] If the phone cannot connect, run this ONCE as admin:
echo        netsh advfirewall firewall add rule name=%FWNAME% dir=in action=allow protocol=TCP localport=%PORT%
goto :launch

rem ---- 3) Start the supervised background service ----
rem service.py start returns 0 when the port is already listening
rem (either it just started it, or it was already up).
:launch
echo.
echo [INFO] Starting the background service ...
set "PYTHONIOENCODING=utf-8"
python "%APP_DIR%\service.py" start --port %PORT%
if errorlevel 1 goto :start_failed

echo.
echo [INFO] Phone: open https://LAN_IP:%PORT%/ or scan the QR window / qr.png
echo        First visit shows a certificate warning - tap Advanced, then Proceed.
echo        (HTTPS is required: phone browsers only expose the microphone on
echo         secure pages, so the mic permission prompt appears only over https.)
echo.
echo [INFO] This service keeps running after you close this window.
echo        Stop: run the STOP .bat next to this file
echo        Or read logs:  python "%APP_DIR%\service.py" logs
echo ------------------------------------------------------------
start "" "%APP_DIR%\qr.png"
ping -n 6 127.0.0.1 >nul
exit /b 0

:start_failed
echo.
echo [ERROR] The background service did not start.
echo         Common cause: port %PORT% is held by ANOTHER program.
echo         Check with:  netstat -ano ^| findstr ":%PORT% "
echo         Then re-run: python "%APP_DIR%\service.py" status
ping -n 12 127.0.0.1 >nul
exit /b 1

:no_dir
echo [ERROR] Server folder not found: %APP_DIR%
echo         Keep this .bat next to the server\ folder after unpacking.
ping -n 10 127.0.0.1 >nul
exit /b 1

rem ---- Environment snapshot, no service started, no UAC, no GUI ----
:selftest
echo ============ Launcher self-test (nothing will be started) ============
echo APP_DIR    = %APP_DIR%
if exist "%APP_DIR%\main.py" (echo main.py    = FOUND) else (echo main.py    = MISSING)
if exist "%APP_DIR%\service.py" (echo service.py = FOUND) else (echo service.py = MISSING)
python --version
python -c "import aiohttp,PIL,win32com.client; print('core deps  = OK')" 2>nul || echo core deps  = MISSING
python -c "import cryptography; print('crypto     = OK (HTTPS enabled)')" 2>nul || echo crypto     = MISSING (service falls back to HTTP, mic permission will NOT appear)
python -c "import qrcode; print('qrcode     = OK (main encoder)')" 2>nul || echo qrcode     = MISSING (built-in fallback encoder)
python -c "import winrt.windows.media.control; print('smtc       = OK (real progress)')" 2>nul || echo smtc       = MISSING (progress falls back to estimate)
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1 && echo port %PORT%  = LISTENING || echo port %PORT%  = free
net session >nul 2>&1 && echo privilege = ADMIN || echo privilege = USER
netsh advfirewall firewall show rule name=%FWNAME% >nul 2>&1 && echo firewall  = rule exists || echo firewall  = rule absent
echo ============ self-test done ============
exit /b 0
