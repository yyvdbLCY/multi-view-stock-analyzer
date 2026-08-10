#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  MVSA 手機版（Termux）「一鍵安裝」腳本
#  用途: 在 Android 手機安裝抓 YouTube 字幕 + 上傳雲端所需的全部工具
#  安裝後: 用 tpush.sh 貼網址 → 抓字幕 → 上傳雲端 → MiniMax 分析 → Telegram
#
#  使用方法（在 Termux 內執行一次）:
#     bash termux_setup.sh
# =====================================================================
set -e
echo "============================================"
echo " MVSA 手機 (Termux) 一鍵安裝"
echo "============================================"

echo "[1/6] 更新 Termux 套件索引..."
pkg update -y || true

echo "[2/6] 安裝 python / node / quickjs / ffmpeg / curl..."
pkg install -y python nodejs-lts ffmpeg curl

echo "[3/6] 安裝/更新 yt-dlp..."
pip install -U --quiet yt-dlp

echo "[4/6] 檢查 JS runtime (quickjs=qjs, 新版 yt-dlp 抓 YouTube 必要)..."
qjs --help >/dev/null 2>&1 && echo "     quickjs 可用: $(qjs --help 2>&1 | head -1 || echo qjs)" || echo "     (quickjs 未裝, 改用 deno/node 亦可)"
deno --version >/dev/null 2>&1 && echo "     deno 也可用"
node --version >/dev/null 2>&1 && echo "     node 可用"

echo "[5/6] 建立 Token 儲存檔 (輸入一次, 之後自動讀)..."
CONFIG_DIR="$HOME/.config/mvsa"
mkdir -p "$CONFIG_DIR"
TOKEN_FILE="$CONFIG_DIR/ingest.secret"
if [ ! -s "$TOKEN_FILE" ]; then
    echo "請輸入你的 INGEST_SECRET (雲端 Token), 只輸入這一次, 之後會自動記住:"
    read -r -p "Token: " TOKEN
    printf '%s' "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "✓ 已存到 $TOKEN_FILE"
else
    echo "✓ 已存在 Token 檔 (如要重設, 刪掉 $TOKEN_FILE 再跑一次)"
fi

echo "[6/6] 撿查完成。接下來用 tpush.sh 抓字幕+上傳。"
echo "============================================"
echo " 用法:  bash tpush.sh <YouTube網址>"
echo "============================================"
