@echo off
echo ===================================================
echo   EasyAIoT Deployment Wizard Startup (Windows)
echo ===================================================
echo Checking Python status...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found in your system PATH.
    echo Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

echo Starting configuration wizard backend...
start "EasyAIoT Deployment Wizard" /b python app.py

echo Waiting for server to start and opening web page...
timeout /t 2 /nobreak >nul
start http://localhost:8899/

echo.
echo Wizard is running! Access URL: http://localhost:8899/
echo To close the wizard, please close this command window.
echo.
pause
