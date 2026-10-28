@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto error
)
if not exist ".venv\proxyhub-installed" (
  .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
  if errorlevel 1 goto error
  type nul > .venv\proxyhub-installed
)
.venv\Scripts\python.exe -m proxyhub
if errorlevel 1 goto error
exit /b 0
:error
echo.
echo ProxyHub could not start. See the error above.
pause
exit /b 1
