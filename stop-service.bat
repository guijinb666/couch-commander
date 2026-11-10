@echo off
setlocal
chcp 65001 >nul 2>&1
title Couch Commander - Stop Service

rem ============================================================
rem  Stops the background service started by the START .bat next to this file.
rem  The service runs detached (no console), so "closing a window"
rem  is not how you stop it - this file is.
rem  This file is ASCII-only on purpose (any cmd codepage is safe).
rem ============================================================

set "ROOT=%~dp0"
set "APP_DIR=%ROOT%server"
set "PORT=8765"

if not exist "%APP_DIR%\service.py" goto :no_dir
cd /d "%APP_DIR%"
if errorlevel 1 goto :no_dir

echo ============================================================
echo    Stopping the Couch Commander service ...
echo ============================================================
set "PYTHONIOENCODING=utf-8"
python "%APP_DIR%\service.py" stop --port %PORT%
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
  echo [OK] Service stopped. Port %PORT% is free again.
) else (
  echo [WARN] Stop returned %RC%. Check what still listens on port %PORT%:
  echo        netstat -ano ^| findstr ":%PORT% "
)
ping -n 8 127.0.0.1 >nul
exit /b %RC%

:no_dir
echo [ERROR] Server folder not found: %APP_DIR%
echo         Keep this .bat next to the server\ folder after unpacking.
ping -n 10 127.0.0.1 >nul
exit /b 1
