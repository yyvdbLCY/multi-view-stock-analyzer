@echo off
REM =====================================================================
REM  MVSA Home PC one-click setup/repair  (Windows 10/11 + Python 3.11+)
REM  Installs/updates: yt-dlp, ffmpeg, and a JavaScript runtime (Deno).
REM  Then runs a quick test to confirm subtitles can be fetched.
REM  USAGE: run on home PC in CMD:  setup_mvsa.bat
REM  Note: new yt-dlp (2026+) REQUIRES a JS runtime for YouTube.
REM        This file installs Deno. All text is ASCII to avoid
REM        code-page mojibake on any Windows locale.
REM =====================================================================
setlocal enabledelayedexpansion
title MVSA one-click setup/repair

echo ============================================
echo  MVSA Home Environment one-click setup/repair
echo ============================================
echo.

echo [1/6] Checking Python and pip...
where python >nul 2>&1
if errorlevel 1 (
    echo       python command not found, trying 'py' launcher...
    where py >nul 2>&1
    if errorlevel 1 (
        echo      [ERROR] Python not found. Please install Python 3.11+ first.
        pause
        exit /b 1
    )
    set "PYEXE=py"
) else (
    set "PYEXE=python"
)
echo      Using %PYEXE%
%PYEXE% --version
%PYEXE% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo      [TIP] Make sure 'Add python.exe to PATH' was ticked at install, or run:
    echo              python -m ensurepip
    pause
    exit /b 1
)
echo      pip OK
echo.

echo [2/6] Upgrading/installing yt-dlp (latest)...
%PYEXE% -m pip install -U --quiet yt-dlp
if errorlevel 1 (
    echo      [ERROR] yt-dlp install failed.
    pause
    exit /b 1
)
echo      yt-dlp updated.
echo.

echo [3/6] Installing JavaScript runtime (Deno, required by new yt-dlp)...
where deno >nul 2>&1
if not errorlevel 1 goto deno_present
where winget >nul 2>&1
if not errorlevel 1 goto deno_winget
echo      winget not found, using official install script...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://deno.land/install.ps1 | iex"
echo      Official Deno script ran. Default install path is
echo       %USERPROFILE%\.deno\bin\deno.exe   (the GUI looks there automatically)
goto deno_done

:deno_present
echo      Deno already present:
deno --version
goto deno_done

:deno_winget
echo      installing Deno via winget...
winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements --silent
if errorlevel 1 goto deno_script_fallback
echo      Deno installed via winget (open a NEW CMD window for PATH to apply).
goto deno_done

:deno_script_fallback
echo      winget Deno install failed, using official script...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://deno.land/install.ps1 | iex"
echo      Official Deno script ran. Default install path is
echo       %USERPROFILE%\.deno\bin\deno.exe   (the GUI looks there automatically)

:deno_done
echo.

echo [4/6] Checking ffmpeg...
where ffmpeg >nul 2>&1
if not errorlevel 1 goto ffmpeg_present
where winget >nul 2>&1
if not errorlevel 1 goto ffmpeg_winget
echo      winget not found; ffmpeg is optional for subtitles-only.
echo      To install later run:  winget install Gyan.FFmpeg
goto ffmpeg_done

:ffmpeg_present
echo      ffmpeg already present:
ffmpeg -version 2>nul | findstr /B "ffmpeg version"
goto ffmpeg_done

:ffmpeg_winget
echo      installing ffmpeg via winget...
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements --silent

:ffmpeg_done
echo.

echo [5/6] Adding freshly-installed tool dirs to THIS window's PATH...
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "NEWPATH=%%B"
set "PATH=%PATH%;%USERPROFILE%\.deno\bin;%USERPROFILE%\.local\bin;%NEWPATH%"
where deno >nul 2>&1 && echo      deno usable now: && deno --version
if errorlevel 1 echo      (deno still not found; open a NEW CMD window and re-run this file)
echo.

echo [6/6] Verifying: try to fetch subtitles for a test video...
echo      (If YouTube asks for sign-in it will fail; see 'cookies' notes below)
%PYEXE% -m yt_dlp --js-runtimes deno --skip-download --write-auto-sub --write-sub --sub-langs "en.*" --sub-format vtt --no-playlist --socket-timeout 20 "https://www.youtube.com/watch?v=nLZ-C7bbZzs" -o "%TEMP%\mvsa_test.%(id)s.%(ext)s" > "%TEMP%\mvsa_test_log.txt" 2>&1
type "%TEMP%\mvsa_test_log.txt"
echo ============================================
echo  Done. If you see subtitle writes or "Downloading" above it's OK.
echo  If you see "Sign in to confirm you're not a bot" or ERROR,
echo  YouTube is blocking unauthenticated fetch; use cookies (below).
echo  If you see "No supported JavaScript runtime", Deno did not install;
echo  open a NEW CMD window and re-run this file, or:  winget install DenoLand.Deno
echo ============================================
echo.
echo  ---- Next steps ----
echo   1) Run  fetch_and_push_gui.pyw  (graphical tool)
echo   2) Make sure INGEST_SECRET is set (see comments in that file)
echo   3) Paste a YouTube URL and press Start
echo.
echo  ---- Cookies fallback (if blocked as bot) ----
echo   Log into YouTube in your browser, export cookies to cookies.txt,
echo   and add to the yt-dlp command:  --cookies cookies.txt
echo   (or add "--cookies", "path\\cookies.txt" in fetch_and_push_gui.pyw)
echo.
pause
