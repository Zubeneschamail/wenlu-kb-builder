@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.cmd first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m kbtool build --source examples\linzhou\source --output output\linzhou-demo.wlkb --customer-id demo-linzhou --name linzhou-demo
if errorlevel 1 goto failed
echo Build complete. Open start.cmd to search the package.
pause
exit /b 0
:failed
echo Build failed. See error above.
pause
exit /b 1
