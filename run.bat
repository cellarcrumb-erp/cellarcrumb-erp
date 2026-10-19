@echo off
title Cellar ^& Crumb ERP
echo =========================================================
echo  Starting Cellar ^& Crumb Operations ERP...
echo.
echo  Open your browser and navigate to: http://localhost:5000
echo =========================================================
echo.
set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY_EXE%" (
    "%PY_EXE%" "%~dp0run_production.py"
) else (
    python "%~dp0run_production.py"
)
pause
