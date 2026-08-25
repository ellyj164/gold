@echo off
REM Build the desktop application and its Windows installer.
REM
REM Produces:
REM   dist\GoldAssistant\GoldAssistant.exe        portable one-folder build
REM   dist\GoldAssistant-Setup-1.1.0.exe          installer
REM
REM Run from the project root:   packaging\build.bat

setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: virtual environment not found. Create it first:
    echo     python -m venv --system-site-packages .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    exit /b 1
)

echo.
echo [1/5] Checking build tools...
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo       Installing PyInstaller...
    ".venv\Scripts\python.exe" -m pip install --quiet pyinstaller || exit /b 1
)

echo.
echo [2/5] Running the test suites...
REM Never package a build that cannot pass its own behaviour tests. Each suite
REM pins behaviour that has been broken by accident before.
for %%T in (test_engines test_signals test_trader test_resolution) do (
    ".venv\Scripts\python.exe" -m tests.%%T >nul
    if errorlevel 1 (
        echo ERROR: tests.%%T failed. Run it directly to see which check broke:
        echo     .venv\Scripts\python.exe -m tests.%%T
        exit /b 1
    )
    echo       tests.%%T passed
)

echo.
echo [3/5] Building the application bundle (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller packaging\GoldAssistant.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 (
    echo ERROR: the PyInstaller build failed.
    exit /b 1
)

echo.
echo [4/5] Building the installer...

REM Inno Setup installs to a different place depending on how it was obtained.
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
    echo       Inno Setup not found - skipping the installer.
    echo       Install it with:  winget install JRSoftware.InnoSetup
    echo.
    echo       The portable build is ready at: dist\GoldAssistant\
    exit /b 0
)

"%ISCC%" packaging\installer.iss
if errorlevel 1 (
    echo ERROR: the installer build failed.
    exit /b 1
)

echo.
echo [5/5] Verifying the packaged application...
".venv\Scripts\python.exe" -m tests.test_packaged
if errorlevel 1 (
    echo ERROR: the packaged application failed its smoke test.
    exit /b 1
)

echo.
echo Done.
echo   Portable : dist\GoldAssistant\GoldAssistant.exe
echo   Installer: dist\GoldAssistant-Setup-1.1.0.exe
echo.
endlocal
