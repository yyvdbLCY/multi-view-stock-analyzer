@echo off
chcp 65001 >nul
REM =====================================================================
REM  MVSA 家用電腦「一鍵安裝/修復」工具  (Windows 11 + Python 3.11+)
REM  安裝/更新: yt-dlp, ffmpeg, JavaScript runtime (Deno)
REM  最後自動測一支測試影片, 確認字幕能抓。
REM  用法: 在 CMD 中執行  setup_mvsa.bat
REM  ※ 本檔已加 chcp 65001, 中文可正常顯示。
REM  ※ 新 yt-dlp (2026+) 抓 YouTube【必須】有 JS runtime, 本檔會裝 Deno。
REM =====================================================================
setlocal enabledelayedexpansion
title MVSA 一鍵安裝/修復

echo ============================================
echo  MVSA 家用環境一鍵安裝/修復
echo ============================================
echo.

echo [1/6] 檢查 Python 與 pip...
where python >nul 2>&1
if errorlevel 1 (
    echo      找不到 python 指令, 改用 py 啟動器...
    where py >nul 2>&1
    if errorlevel 1 (
        echo      [錯誤] 找不到 Python。請先安裝 Python 3.11+ 再執行本檔。
        pause
        exit /b 1
    )
    set "PYEXE=py"
) else (
    set "PYEXE=python"
)
echo     使用 %PYEXE%
%PYEXE% --version
%PYEXE% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo      [提示] 請確認已勾選 "Add python.exe to PATH"，或執行:
    echo              python -m ensurepip
    pause
    exit /b 1
)
echo      pip OK
echo.

echo [2/6] 升級/安裝 yt-dlp (最新版)...
%PYEXE% -m pip install -U --quiet yt-dlp
if errorlevel 1 (
    echo      [錯誤] yt-dlp 安裝失敗。
    pause
    exit /b 1
)
echo      yt-dlp 更新完成.
echo.

echo [3/6] 安裝 JavaScript runtime (Deno, 新 yt-dlp 抓 YouTube 必要)...
REM 優先 winget (可靠, 會自動加 PATH), 失敗再用官方 script。
set "DENO=deno"
%DENO% --version >nul 2>&1
if errorlevel 1 (
    where deno >nul 2>&1
    if errorlevel 1 (
        echo      尚未偵測到 deno, 嘗試 winget 安裝...
        where winget >nul 2>&1
        if errorlevel 1 (
            echo      [警告] 未偵測到 winget, 改用官方安裝腳本...
            goto :deno_script
        )
        winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements --silent
        if errorlevel 1 (
            echo      winget 安裝 Deno 失敗, 改用官方腳本...
            goto :deno_script
        ) else (
            echo      Deno 已用 winget 安裝 (path 需重新開視窗才生效)。
            goto :deno_done
        )
    )
) else (
    echo      Deno 已存在:
    %DENO% --version
    goto :deno_done
)
:deno_script
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://deno.land/install.ps1 | iex"
echo      Deno 官方腳本已執行。預設裝到:
echo       %%USERPROFILE%%\.deno\bin\deno.exe   (fetch_and_push_gui 會自動找這個路徑)
:deno_done
echo.

echo [4/6] 檢查 ffmpeg...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo      尚未安裝 ffmpeg, 用 winget 安裝中...
    where winget >nul 2>&1
    if errorlevel 1 (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/GyanD/codexffmpeg/releases/latest/download/ffmpeg-release-essentials.zip -OutFile $env:TEMP\ffmpeg.zip; Expand-Archive $env:TEMP\ffmpeg.zip -DestinationPath $env:TEMP\ffmpeg_extract -Force; Copy-Item $env:TEMP\ffmpeg_extract\*\bin\* $env:USERPROFILE\.local\bin -Recurse -Force; [System.IO.Directory]::CreateDirectory(\"$env:USERPROFILE\.local\bin\") | Out-Null"
        echo      ffmpeg 已下載並加入 %%USERPROFILE%%\.local\bin
    ) else (
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements --silent
    )
) else (
    echo      ffmpeg 已存在:
    ffmpeg -version 2>nul | findstr /B "ffmpeg version"
)
echo.

echo [5/6] 設定本視窗一時的 PATH (讓剛裝的 deno/ffmpeg 立即可測)...
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "NEWPATH=%%B"
set "PATH=%PATH%;%USERPROFILE%\.deno\bin;%USERPROFILE%\.local\bin;%NEWPATH%"
where deno >nul 2>&1 && echo      deno 可用: && deno --version
if errorlevel 1 echo      (deno 仍找不到, 代表未安裝成功, 請重開視窗後再跑一次)
echo.

echo [6/6] 驗證: 試抓一支測試影片的字幕...
echo      (若 YouTube 要求登入會失敗, 請見本文檔底部「cookies」說明)
%PYEXE% -m yt_dlp --js-runtimes deno --skip-download --write-auto-sub --write-sub --sub-langs "en.*" --sub-format vtt --no-playlist --socket-timeout 20 "https://www.youtube.com/watch?v=nLZ-C7bbZzs" -o "%TEMP%\mvsa_test.%(id)s.%(ext)s" > "%TEMP%\mvsa_test_log.txt" 2>&1
type "%TEMP%\mvsa_test_log.txt"
echo ============================================
echo  完成. 若上方看到字幕寫入或 "Downloading" 即表示 OK。
echo  若見到 "Sign in to confirm you're not a bot" 或 ERROR,
echo  代表 YouTube 擋未登入抓取, 需設定 cookies (見下)。
echo  ※ 若見到 "No supported JavaScript runtime" 表示 Deno 沒裝成功,
echo     請重開一個 CMD 視窗再跑一次本檔, 或用 winget install DenoLand.Deno
echo ============================================
echo.
echo  ---- 接下來怎麼用 ----
echo   1) 執行  fetch_and_push_gui.pyw  (圖形版)
echo   2) 確認 INGEST_SECRET 已設定 (見該檔註解)
echo   3) 貼 YouTube 網址按下開始
echo.
echo  ---- cookies 備案 (若被擋 bot) ----
echo   在被擋的瀏覽器登入 YouTube, 匯出 cookies 檔,
echo   抓片時加參數: --cookies cookies.txt
echo   或改在 fetch_and_push_gui.pyw 的 yt-dlp 指令加:
echo       "--cookies", "你的cookies.txt路徑",
echo.
pause
