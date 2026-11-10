@echo off
REM ============================================================
REM  Couch Commander - foreground dev launcher (Windows)
REM  Creates .venv, installs deps, runs main.py in this console.
REM  For always-on background use the root launcher instead:
REM    ..\one-click-start.bat   (service.py start)
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python 3.10+ not found. Install it and enable "Add python.exe to PATH".
        goto :fail
    )
    set "PY=py -3"
)
echo [1/4] interpreter: %PY%

if not exist ".venv\Scripts\python.exe" (
    echo [2/4] creating virtualenv .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] failed to create virtualenv.
        goto :fail
    )
) else (
    echo [2/4] virtualenv already exists, skipping.
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo [3/4] installing requirements.txt ...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 echo [WARN] pip upgrade failed, continuing.
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] dependency install failed. Try manually:
    echo         "%VPY%" -m pip install -r requirements.txt
    goto :fail
)

echo [4/4] starting server on port 8765 ^(Ctrl+C to stop^) ...
echo.
"%VPY%" main.py %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo [SERVER] exited with code %EXITCODE%
) else (
    echo [SERVER] stopped normally.
)
pause
endlocal
exit /b %EXITCODE%

:fail
echo.
echo Startup did not complete. Fix the message above and retry.
pause
endlocal
exit /b 1
