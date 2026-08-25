@echo off
REM Launcher for the AI Gold Trading Decision Assistant.
REM Double-click this file to start the terminal.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing.
    echo Run the following once, from this folder:
    echo.
    echo     python -m venv --system-site-packages .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Starting the Gold Decision Assistant...
echo Make sure MetaTrader 5 is open and logged in.
echo.

".venv\Scripts\pythonw.exe" -m goldassist.app
if errorlevel 1 (
    echo.
    echo The application exited with an error. Check the log at:
    echo   %USERPROFILE%\.goldassist\goldassist.log
    pause
)
