#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  MVSA 手機版 (Termux) 「抓字幕 + 上傳雲端」主程式
#  用法:
#     bash tpush.sh <YouTube網址>
#     bash tpush.sh 一   （不帶網址時, 自動讀剪貼簿）
#  會做: 抓到 en 自動字幕 → 上傳到雲端 ingest → 雲端 MiniMax 分析 → TG
#
#  Token: 存在 $HOME/.config/mvsa/ingest.secret (由 termux_setup.sh 建立)
# =====================================================================
set -u

# ---- 雲端接收器設定 (可改, 預設公網) ----
INGEST_URL="${INGEST_URL:-http://43.156.50.109:9000/ingest}"
CONFIG_DIR="$HOME/.config/mvsa"
TOKEN_FILE="$CONFIG_DIR/ingest.secret"
WORKDIR="${TMPDIR:-/tmp}/mvsa_phone"

# ---- 讀 Token (自動) ----
if [ ! -s "$TOKEN_FILE" ]; then
    echo "❌ 找不到 Token 檔 $TOKEN_FILE"
    echo "   請先執行: bash termux_setup.sh" 
    exit 1
fi
SECRET=$(cat "$TOKEN_FILE" | tr -d '\r\n')

# ---- 取得網址 (參數 > 剪貼簿) ----
URL="${1:-}"
if [ -z "$URL" ]; then
    URL=$(termux-clipboard-get 2>/dev/null | tr -d '\r\n')
    echo "📋 從剪貼簿讀到: $URL"
fi
if [ -z "$URL" ]; then
    echo "❌ 沒有網址。請用:  bash tpush.sh <網址>"
    echo "   或先把網址複製, 再直接執行 bash tpush.sh"
    exit 1
fi

# ---- 偵測 JS runtime (新版 yt-dlp 必需要) ----
if command -v deno >/dev/null 2>&1; then
    JS_RT="deno"
elif command -v node >/dev/null 2>&1; then
    JS_RT="node"
elif command -v qjs >/dev/null 2>&1; then
    JS_RT="quickjs"
else
    echo "❌ 沒有 JS runtime。請先 bash termux_setup.sh 安裝 (node/qjs/deno)"
    exit 1
fi
echo "▶ 使用 JS runtime: $JS_RT"

# ---- 抓字幕 ----
echo "⟳ 抓字幕: $URL"
mkdir -p "$WORKDIR"
cmd="python -m yt_dlp --js-runtimes $JS_RT --skip-download --write-auto-sub --write-sub"
cmd="$cmd --sub-langs en.* --sub-format vtt --write-info-json --no-playlist"
cmd="$cmd --socket-timeout 20 -o \"$WORKDIR/%(id)s.%(ext)s\" \"$URL\""
echo "  $cmd"
bash -c "$cmd" > "$WORKDIR/dl.log" 2>&1

SUB=$(ls "$WORKDIR"/*.en*.vtt 2>/dev/null | head -1)
if [ -z "$SUB" ]; then SUB=$(ls "$WORKDIR"/*.vtt 2>/dev/null | head -1); fi
if [ -z "$SUB" ]; then
    echo "❌ 沒抓到字幕。log 尾段:"
    tail -8 "$WORKDIR/dl.log"
    exit 1
fi
CHARS=$(wc -c < "$SUB")
echo "✓ 字幕 $CHARS 字元 ($SUB)"

# ---- 組 payload (用 python 解析 info.json + vtt → json) ----
PAYLOAD="$WORKDIR/payload.json"
python - "$URL" "$WORKDIR" "$SUB" "$SECRET" <<'PY' > /dev/null
import json, re, sys, os, html
url, WDIR, sub, secret = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
# 讀 info.json
info={}
m=[f for f in os.listdir(WDIR) if f.endswith('.info.json')]
if m:
    try: info=json.load(open(os.path.join(WDIR,m[0]),encoding='utf-8'))
    except: pass
# 讀 vtt → text
def clean_vtt(path):
    out=[]
    for line in open(path,encoding='utf-8',errors='ignore'):
        line=line.strip()
        if not line or '-->' in line or line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:') or line.isdigit():
            continue
        line=re.sub(r'<[^>]+>','',line)
        line=html.unescape(line)
        if line: out.append(line)
    return '\n'.join(out)
text=clean_vtt(sub)
# 找 video id
vid=re.search(r'(?:v=|\.be/)([A-Za-z0-9_-]{11})', url)
payload={
    "youtube_url": url,
    "video_id": vid.group(1) if vid else "",
    "title": info.get('title',''),
    "channel": info.get('channel','') or info.get('uploader',''),
    "published": info.get('upload_date',''),
    "transcript": text,
    "caption_source": "termux-phone",
}
json.dump(payload, open(WDIR+'/payload.json','w',encoding='utf-8'), ensure_ascii=False)
print("done")
PY

# ---- 上傳雲端 ----
echo "⟳ 上傳到雲端..."
HTTP=$(curl -s -o "$WORKDIR/resp.txt" -w "%{http_code}" --max-time 60 \
    -X POST "$INGEST_URL" \
    -H "Authorization: Bearer $SECRET" \
    -H "Content-Type: application/json" \
    --data-binary @"$PAYLOAD")
echo "    HTTP $HTTP"
echo "    雲端回應: $(cat "$WORKDIR/resp.txt")"

if [ "$HTTP" = "200" ]; then
    echo "✅ 已送達雲端, 稍後 Telegram (牛牛GO) 會收到 MiniMax 分析。"
else
    echo "⚠️  上傳未成功 (HTTP $HTTP)。請檢查 Token 與網路。"
fi
