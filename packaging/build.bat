@echo off
REM Build the desktop application and its Windows installer.
REM
REM Produces:
REM   dist\GoldAssistant\GoldAssistant.exe        portable one-folder build
REM   dist\GoldAssistant-Setup-<version>.exe      installer
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

REM The version is defined once, in goldassist/__init__.py, and read from
REM there. It used to be typed separately here, in installer.iss and in two
REM echo lines, which is four places to forget.
REM Via a temp file rather than `for /f`. The command needs quotes around both
REM the interpreter path (it contains spaces) and the -c argument, and every
REM form of `for /f` quoting mangles one or the other -- the plain ('...')
REM form splits the path at its first space. Writing and reading a file has
REM no quoting rules to get wrong.
".venv\Scripts\python.exe" -c "import goldassist; print(goldassist.__version__)" > "%TEMP%\goldassist-version.txt"
set /p APPVER=<"%TEMP%\goldassist-version.txt"
del "%TEMP%\goldassist-version.txt" >nul 2>&1
if "%APPVER%"=="" (
    echo ERROR: could not read the version from goldassist/__init__.py
    exit /b 1
)
echo Building Gold Decision Assistant %APPVER%

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
for %%T in (test_engines test_signals test_trader test_resolution test_timeline test_analysis_v12 test_ai_settings test_costs) do (
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
REM The intermediate build tree goes to %TEMP%, not into the project.
REM
REM This project lives under OneDrive, and OneDrive's Files On-Demand turns
REM synced folders into reparse points. PyInstaller deletes its output tree
REM before rewriting it, and deleting a dehydrated placeholder fails with
REM "WinError 5: Access is denied" -- which is what killed a build here, two
REM hundred directories deep inside the bundled anthropic package. The work
REM tree is thousands of transient files and has no business being synced to
REM the cloud at all, so it is built locally.
REM
REM `dist` stays put: it holds the artefacts worth keeping. If the same error
REM appears on `dist\GoldAssistant`, delete that folder by hand and re-run --
REM OneDrive will have dehydrated the previous build.
set "WORKDIR=%TEMP%\goldassist-build"
".venv\Scripts\python.exe" -m PyInstaller packaging\GoldAssistant.spec --noconfirm --distpath dist --workpath "%WORKDIR%"
if errorlevel 1 (
    echo ERROR: the PyInstaller build failed.
    echo.
    echo If this was "WinError 5: Access is denied" under dist\GoldAssistant,
    echo OneDrive has dehydrated the previous build. Remove it and retry:
    echo     rd /s /q dist\GoldAssistant
    exit /b 1
)

echo.
echo [4/5] Verifying the packaged application...
REM Runs BEFORE the installer, not after.
REM
REM This step used to be [5/5]. When Inno Setup is absent -- as it is on the
REM development machine -- step 4 exited with success and took the smoke test
REM with it, so the one gate that catches windowed-build-only failures never
REM ran on the only machine that builds. Those failures (a null sys.stdout
REM under --noconsole crashing uvicorn's log formatter) appear in
REM GoldAssistant.exe and never in GoldAssistant-debug.exe, which is exactly
REM what the smoke test checks and exactly what a skipped step hides.
".venv\Scripts\python.exe" -m tests.test_packaged
if errorlevel 1 (
    echo ERROR: the packaged application failed its smoke test.
    exit /b 1
)

echo.
echo [5/5] Building the installer...

REM Inno Setup installs to a different place depending on how it was obtained.
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
    echo       Inno Setup not found - skipping the installer.
    echo       Install it with:  winget install JRSoftware.InnoSetup
    echo.
    echo       The portable build is ready and smoke-tested at: dist\GoldAssistant\
    exit /b 0
)

"%ISCC%" /DAppVersion=%APPVER% packaging\installer.iss
if errorlevel 1 (
    echo ERROR: the installer build failed.
    exit /b 1
)

echo.
echo Done.
echo   Portable : dist\GoldAssistant\GoldAssistant.exe
echo   Installer: dist\GoldAssistant-Setup-%APPVER%.exe
echo.
endlocal
