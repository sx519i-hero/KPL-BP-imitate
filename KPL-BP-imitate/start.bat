@echo off
REM ======================================================================
REM  KPL BP Coach - one-click launcher for Windows
REM
REM  IMPORTANT: THIS FILE IS INTENTIONALLY PURE ASCII (no Chinese at all).
REM
REM  Two traps this file avoids:
REM
REM  1) cmd.exe parses a .bat using the CURRENT console code page. A .bat
REM     containing multi-byte Chinese under CP936 gets its quotes/parens
REM     split and dies with "is not recognized as an internal or external
REM     command" BEFORE reaching the chcp line - the server never starts.
REM     (Reproduced: the UTF-8 version of this launcher always failed.)
REM     => keep this file ASCII-only; Chinese text lives in launcher.py.
REM
REM  2) `for /f ... in ('"%PY%" -c "..."')` mangles a QUOTED executable
REM     path: cmd strips the outer quotes and you get
REM     'D:\...\python.exe" -c "import' is not recognized.
REM     => never capture output from a quoted exe in for /f here; let
REM     launcher.py probe <exe> do the version check and report it.
REM ======================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
REM v9: listen host/port come from data\server_config.json (see launcher.py config_port)
set "PORT=5000"
title KPL BP Coach
cd /d "%~dp0"

echo ======================================================================

REM ---------- 1. locate Python ----------
REM Try, in order: the py launcher, `python` on PATH, then common install
REM locations. Each candidate is verified by launcher.py probe (it must
REM actually run and print a version) - a broken Microsoft Store stub on
REM PATH therefore cannot pass.
set "PY="

REM Real interpreter first: `where python` returns full paths, so PY is always
REM a single token that can be safely quoted. (The Microsoft Store stub also
REM shows up here, so every candidate is executed once to prove it works.)
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY (
        "%%i" -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY=%%i"
    )
)

REM Then the py launcher (py.exe is a real path too, no arguments needed).
if not defined PY (
    for /f "delims=" %%i in ('where py 2^>nul') do (
        if not defined PY (
            "%%i" -c "import sys" >nul 2>&1
            if not errorlevel 1 set "PY=%%i"
        )
    )
)

REM Finally probe the usual install roots, in case Python was installed
REM without "Add Python to PATH" - otherwise the user sees a scary
REM "Python not found" even though it is right there on disk.
if not defined PY (
    for %%r in (
        "%LOCALAPPDATA%\Programs\Python"
        "%ProgramFiles%"
        "%ProgramFiles(x86)%"
        "C:\"
    ) do (
        if exist "%%~r" (
            for /d %%d in ("%%~r\Python3*") do (
                if not defined PY if exist "%%~d\python.exe" (
                    "%%~d\python.exe" -c "import sys" >nul 2>&1
                    if not errorlevel 1 set "PY=%%~d\python.exe"
                )
            )
        )
    )
)

if not defined PY goto :no_python

REM Verify + report the version (launcher.py handles the quoting).
"%PY%" launcher.py probe "%PY%"
if errorlevel 1 goto :no_python

REM ---------- 1b. virtual environment ----------
REM Use a local .venv so the game never touches the user's global packages.
REM If creating it fails (no venv module, offline wheelhouse, exotic Python),
REM fall back to the system interpreter instead of aborting.
REM
REM NOTE: use !RUNPY! (delayed expansion) inside these blocks - a plain
REM %RUNPY% is substituted when the whole if-block is parsed, i.e. BEFORE
REM the set runs, so it would still hold the previous value.
set "RUNPY=%PY%"
if exist ".venv\Scripts\python.exe" (
    REM Probe it: an interrupted venv creation can leave a broken python.exe.
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        rmdir /s /q .venv >nul 2>&1
    ) else (
        set "RUNPY=.venv\Scripts\python.exe"
        "!RUNPY!" launcher.py venv_ready
    )
)

if "!RUNPY!"=="%PY%" (
    "%PY%" launcher.py venv_creating
    "%PY%" -m venv .venv >nul 2>&1
    if exist ".venv\Scripts\python.exe" (
        set "RUNPY=.venv\Scripts\python.exe"
    ) else (
        "%PY%" launcher.py venv_fallback
    )
)

REM ---------- 2. dependencies ----------
"%RUNPY%" -c "import flask, requests, bs4" >nul 2>&1
if errorlevel 1 (
    "%RUNPY%" launcher.py deps_installing
    "%RUNPY%" -m pip install --upgrade pip >nul 2>&1
    "%RUNPY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :deps_fail
    "%RUNPY%" launcher.py deps_done
) else (
    "%RUNPY%" launcher.py deps_ready
)

REM ---------- 3. data ----------
REM The game needs real scraped data. heroes.json must exist and hold more
REM than 50 heroes. v33: startup never scrapes; the player triggers it in-game.
set "NEED_SCRAPE=1"
if exist "data\heroes.json" (
    "%RUNPY%" -c "import json,sys;d=json.load(open('data/heroes.json',encoding='utf-8'));sys.exit(0 if len(d.get('heroes',[]))>50 else 1)" >nul 2>&1
    if not errorlevel 1 set "NEED_SCRAPE=0"
)

if "!NEED_SCRAPE!"=="1" (
    "%RUNPY%" launcher.py data_missing
) else (
    "%RUNPY%" launcher.py data_skip
)

REM ---------- 4. serve ----------
REM If something already listens on the port (a window left open), just open
REM the browser instead of dying with "address already in use".
"%RUNPY%" launcher.py port
if not errorlevel 1 goto :start_server

"%RUNPY%" launcher.py already_running
"%RUNPY%" launcher.py open
timeout /t 4 /nobreak >nul
exit /b 0

:start_server
"%RUNPY%" launcher.py serve
echo ======================================================================
"%RUNPY%" launcher.py serve_lines
echo ======================================================================
echo.

REM Open the browser in a detached helper that waits for the port to come up.
start "" /b "%RUNPY%" launcher.py open_delayed 3

"%RUNPY%" app.py

echo.
"%RUNPY%" launcher.py stopped
pause
exit /b 0

REM ---------- error paths ----------
REM NOTE: in :no_python there is no interpreter to print Chinese with, so the
REM message here must be plain ASCII echoed by cmd itself.
:no_python
echo.
echo ======================================================================
echo  [ERROR] Python not found.
echo ======================================================================
echo.
echo  This game needs Python 3.8 or newer.
echo.
echo    1. Download it from:  https://www.python.org/downloads/
echo    2. During setup, TICK the checkbox "Add python.exe to PATH".
echo    3. Close this window and run start.bat again.
echo.
pause
exit /b 1

:deps_fail
echo.
echo [ERROR] Dependency installation failed. Retry with a China mirror:
echo    "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:scrape_fail
echo.
echo [ERROR] Data scraping failed. Check your network and run this script again.
echo         Already-downloaded pages are cached in .scrape_cache, so a rerun resumes.
echo.
pause
exit /b 1
