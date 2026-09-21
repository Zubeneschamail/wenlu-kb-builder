@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m kbtool prepare-model
if errorlevel 1 goto failed
echo Setup complete. Open start.cmd.
pause
exit /b 0
:failed
echo Setup failed. Check Python 3.12 and the error above.
pause
exit /b 1
