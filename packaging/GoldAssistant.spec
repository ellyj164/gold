# PyInstaller build for the AI Gold Trading Decision Assistant.
#
# Built as a one-folder bundle rather than a single .exe: pandas and numpy make
# a one-file build unpack ~120MB to a temp directory on every launch, which
# costs several seconds of startup and trips some antivirus heuristics.
#
# Build with:  .venv\Scripts\pyinstaller packaging\GoldAssistant.spec --noconfirm

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT = Path(SPECPATH).parent

# The UI is plain files read at runtime, so it must ship as data, not code.
datas = [
    (str(PROJECT / "goldassist" / "ui" / "index.html"), "goldassist/ui"),
    (str(PROJECT / "goldassist" / "ui" / "static"), "goldassist/ui/static"),
    (str(PROJECT / "README.md"), "."),
]

binaries = []
hiddenimports = [
    # uvicorn resolves its protocol implementations by string at runtime, so
    # PyInstaller's import scanner cannot see any of them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    # Both websocket implementations. `auto` already resolves to the sans-io
    # one -- uvicorn's own `websockets_impl` now emits a deprecation warning
    # saying `--ws websockets` will point there in a future release -- so the
    # sans-io module is what actually serves the dashboard's live push today.
    # `websockets_impl` stays listed because `auto` falls back to it, and
    # config.py resolves either by string at runtime (WS_PROTOCOLS), which is
    # precisely what PyInstaller's scanner cannot follow.
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.websockets_sansio_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets.legacy",
    "websockets.legacy.server",
    # Windows-only data source and the desktop window backend.
    "MetaTrader5",
    "webview.platforms.edgechromium",
    "clr_loader",
    "encodings.idna",
]

# Packages whose data files, DLLs or dynamic imports need collecting whole.
#
# `tzdata` is here because Windows ships no IANA timezone database and
# `zoneinfo` therefore reads one out of that package. `analysis/calendar_sync`
# converts release times from US Eastern to UTC through it, which is what
# makes the daylight-saving shift right -- 08:30 Eastern is 12:30 UTC in
# October and 13:30 UTC in November. Without the data files bundled, the
# import still succeeds and the *call* raises ZoneInfoNotFoundError, so this
# is the kind of omission that only shows up in the packaged build.
#
# `websockets` is here for the same class of reason: the sans-io protocol
# implementation reaches into six of its submodules (server, http11, frames,
# datastructures, exceptions, extensions.permessage_deflate), and the whole
# package is small enough that collecting it is cheaper than discovering a
# missing one from a packaged build that cannot open its own dashboard.
for package in ("uvicorn", "anthropic", "httpx", "httpcore", "certifi",
                "fastapi", "starlette", "webview", "MetaTrader5", "tzdata",
                "websockets"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        # A package that is not installed simply contributes nothing.
        pass

hiddenimports += collect_submodules("goldassist")

# Large libraries the application never imports. Excluding them keeps the
# bundle to a reasonable size.
excludes = [
    "tkinter", "matplotlib", "scipy", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gtk", "test", "tests",
    "sqlalchemy", "flask", "pytest",
]

a = Analysis(
    [str(PROJECT / "packaging" / "launcher.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GoldAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed: the UI is the desktop window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT / "packaging" / "goldassistant.ico")
    if (PROJECT / "packaging" / "goldassistant.ico").exists() else None,
)

# A second executable with a console attached. Same code, but it prints the
# log to a terminal — the first thing to run when something misbehaves.
exe_console = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GoldAssistant-debug",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GoldAssistant",
)
