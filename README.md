# Multi-View Stock Analyzer

> **YouTube 美股觀點分析機器人** — 把多個財經 YouTuber 的影片字幕抓下來,用 Gemini 做雙層 LLM 評測,最後透過 Telegram 推送**附置信度的結論**給你。

> 💡 **這是怎麼做出來的** — 從 2026-07-26 用戶給的系統框架(雙層 LLM 評測 + 置信度)開始,Mavis 把它落地成 9 個 Python 模組,並推上 GitHub 作為 MVP。完整開發筆記見 [ARCHITECTURE.md](ARCHITECTURE.md)。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://github.com/astral-sh/ruff)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](.github/workflows/ci.yml)

```
YouTube URL (Telegram 輸入)
   │
   ▼
┌─────────────────────────────────────────┐
│  Layer 1 LLM (Gemini Flash)             │
│  extractor.py — 提取每支影片的 ticker    │
│  與論點 → per-stock JSON                 │
└─────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────┐
│  aggregator.py — 按 ticker 聚合近 7 天   │
│  提及 + yfinance 即時股價                │
└─────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────┐
│  Layer 2 LLM (Gemini Flash)             │
│  evaluator.py — 綜合評測 + 雙置信度評分   │
└─────────────────────────────────────────┘
   │
   ▼
Telegram 推送 (Markdown)
```

> ⚠️ **免責聲明**:本工具僅供個人研究與學習用途。所有 AI 生成的報告**皆不構成投資建議**,投資決策請自行審慎判斷。

---

## 為什麼要這個工具?

YouTube 上有 6 個你常追蹤的美股分析頻道,每個人都講得頭頭是道,但你看完常常想:

- 「這三個人對 TSLA 的看法,到底誰說的比較有道理?」
- 「他們觀點的**一致程度**怎麼樣?」
- 「如果方向一致,**我該多有信心**?」

這個工具就是為了解決這件事。**核心設計**是兩層 LLM 協作:

| 層 | 做什麼 | LLM |
|---|---|---|
| Layer 1 | 把字幕拆成結構化資料 (ticker / sentiment / 論點 / 信心度) | Gemini Flash |
| Layer 2 | 拿同 ticker 在近 7 天的所有觀點,做綜合評測,給出**看好度**和**系統置信度** | Gemini Flash |

`overall_score` (看好度) 與 `confidence` (系統置信度) 是**獨立兩個維度**:可能出現「極度看好但置信度只有 3」的情況 — 這代表「講者很看好,但只有單一來源、缺乏佐證」。

詳細架構 → 看 [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 環境需求

- Python **3.10+**
- `ffmpeg` (僅在啟用 Whisper fallback 時需要)
- 網路可連 YouTube / Telegram / Google AI Studio

---

## 快速開始

### 1. Clone & 安裝

```bash
git clone https://github.com/yyvdbLCY/multi-view-stock-analyzer.git
cd multi-view-stock-analyzer

python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. 取得 API Keys

| 服務 | 怎麼拿 | 用途 |
|---|---|---|
| **Telegram Bot Token** | Telegram 找 [@BotFather](https://t.me/BotFather),`/newbot` 取得 `123456:ABC-DEF...` | Bot 入口 |
| **你的 Telegram User ID** | Telegram 找 [@userinfobot](https://t.me/userinfobot) | 授權白名單 |
| **Gemini API Key** | 到 [Google AI Studio](https://aistudio.google.com/app/apikey) 申請,**免費** | LLM 兩層評測 |

### 3. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env,填入三組 key
```

最小必填:
```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ALLOWED_USER_IDS=你的UserID
GEMINI_API_KEY=AIza...
```

### 4. 啟動 Bot

```bash
python main.py
```

看到 `Bot starting up...` 即可在 Telegram 開始使用。

---

## 使用方式

### 指令

| 指令 | 行為 |
|---|---|
| `/start` | 歡迎訊息 + 用法 |
| `/help` | 完整說明 |
| **貼 YouTube URL** | 跑完整 pipeline:抓字幕 → 提取 → 聚合 → 評測 → 回報告 |
| `/report TSLA` | 重新針對 TSLA 做綜合評測 (用既有 extractions) |
| `/digest` | 對近 7 天**所有**有提及的 ticker 逐一做評測 |

### 範例對話

你:
```
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Bot:
```
📥 收到連結,開始處理...
📥 收到連結,開始處理...
`https://www.youtube.com/watch?v=...`

步驟 1/3: 抓取字幕
```

(幾秒後...)

```
✅ 抓取成功 (auto)
🎬 Tesla Q3 交付數據分析 - 該進場了嗎?
📺 Tesla Daily
🏷️ 提及 2 檔: TSLA, NVDA

步驟 2/3: 聚合近 7 天觀點 + 抓股價
```

(再幾秒...)

```
📊 TSLA  $245.30
• 看好度: 7/10 🟢
• 綜合情緒: cautiously bullish
• 系統置信度: 8/10 ✅
• 樣本數: 4 個提及

綜合 4 個頻道看法,特斯拉短期受交付數據支撐,
但估值已偏高,需關注 Q4 營收指引...

關鍵論點:
  • Q3 交付量超預期 23%
  • FSD 進度加速
  • 中國市場價格戰壓力

風險:
  ⚠️ 估值過高,本益比 75x
  ⚠️ 利率上升影響需求

結論: 短線偏多但建議逢回再進場

⚠️ AI 生成,僅供參考,不構成投資建議
```

---

## 專案結構

```
multi-view-stock-analyzer/
├── main.py              # Telegram Bot 入口
├── config.py            # 環境變數載入
├── prompts.py           # 兩層 Prompt 模板 (核心)
├── youtube_client.py    # yt-dlp 字幕抓取 (manual → auto → Whisper)
├── extractor.py         # Layer 1: Gemini 提取
├── aggregator.py        # 跨影片聚合
├── evaluator.py         # Layer 2: Gemini 綜合評測
├── stock_client.py      # yfinance 即時股價
├── storage.py           # SQLite 持久化
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── ARCHITECTURE.md      # 詳細架構說明
├── LICENSE
├── db/                  # SQLite 檔 (執行時自動生成)
├── reports/             # JSON 報告 (執行時自動生成)
└── logs/                # 執行 log
```

---

## 進階設定

### 🧪 GitHub Actions 雲端 E2E Smoke Test (推薦先用這個驗證 pipeline)

不想本地裝依賴?用 GitHub Actions 在雲端跑一次完整 pipeline:

1. 進 repo → **Actions** tab
2. 選 **"Smoke Test (E2E)"** workflow
3. 點 **"Run workflow"** → 貼個 YouTube URL (或用預設) → 選要不要推 Telegram
4. 等 30-60 秒,下載 artifacts (`transcript.txt` / `extractions.json` / `report.json` / `summary.md`)

**前置條件** (只設一次):
- `GEMINI_API_KEY` (Actions secret)
- `TELEGRAM_BOT_TOKEN` (如果你想推到 Telegram)
- `TELEGRAM_ALLOWED_USER_IDS` (你的 Telegram user ID)

> ⚠️ 注意:這個 workflow 適合偶爾驗證 / 測試 prompt 改動。**不要拿來跑 24/7 bot** — 那是 polling server 場景,GitHub Actions 不適合 hosting 長期服務。

### 換更好的模型 (但更貴)

```env
# 用 Gemini 2.5 Pro 做評測 (品質較好,成本約 10 倍)
GEMINI_MODEL_EVAL=gemini-2.5-pro

# Layer 1 維持 Flash 就好 (純結構化抽取,Flash 夠用)
GEMINI_MODEL_EXTRACT=gemini-2.0-flash
```

### 啟用 Whisper 音訊轉寫

如果某些影片沒有字幕,fallback 到 Whisper 音訊轉寫(較慢):

```bash
# 安裝系統依賴
brew install ffmpeg  # macOS
# 或 sudo apt install ffmpeg  # Linux

# 解除 requirements.txt 內 openai-whisper 註解
pip install openai-whisper

# .env
ENABLE_WHISPER_FALLBACK=true
WHISPER_MODEL=base  # tiny / base / small / medium / large
```

### 改聚合窗口

```env
AGGREGATION_WINDOW_DAYS=14  # 預設 7 天,可改 14/30
```

---

## 開發路線圖 (Roadmap)

- [ ] YouTube Data API v3 自動監控 6 個頻道新片 (從手動推送 → 自動消化)
- [ ] 頻道歷史準確率回測 (動態權重)
- [ ] 多 LLM 投票提升置信度穩健度 (Gemini + Claude + DeepSeek)
- [ ] Web Dashboard (Streamlit) 視覺化歷史曲線
- [ ] cron 排程: 盤前自動 `/digest`
- [ ] RAG ticker/公司名辭典 (字幕校正率)
- [ ] 支援 X / Podcast 輸入

---

## 常見問題

**Q: 為什麼有些影片抓不到字幕?**
A: 該影片可能沒有自動字幕,或字幕被上傳者設為私人。可設定 `ENABLE_WHISPER_FALLBACK=true` 啟用音訊轉寫(較慢但更全面)。

**Q: 同一個 ticker 多次貼影片,報告都一樣嗎?**
A: 報告是即時綜合的,每次都會重新讀取近 7 天所有 extractions。但 extractions 本身是累加的,**不會重複處理同一個影片** (透過 video_id 去重)。

**Q: Gemini API 配額會不會爆?**
A: 免費額度每分鐘 15 次。單支影片通常 1-2 次 Gemini 呼叫 (提取 + N 個 ticker 各一次評測)。`/digest` 大量 ticker 時要小心。

**Q: 我可以改用 Claude / GPT 嗎?**
A: 可以。需要修改 `extractor.py` 和 `evaluator.py` 內的 model 初始化,以及 `prompts.py` 的 JSON 解析邏輯(各家 API 不同)。PR 歡迎。

**Q: confidence 跟 overall_score 差在哪?**
A: 看好度 = 方向 (多還是空)。置信度 = 這個方向**我有多相信**。可能「看好 9 但置信度 3」(單一來源狂推)、「看好 5 但置信度 9」(多方共識中性)。兩個維度獨立。

**Q: 為什麼預設用 Gemini 2.0 Flash?**
A: 免費額度大、JSON 遵從度高、速度快。對字幕拼字錯誤有上下文校正。Pro 版 10x 成本但品質有限提升,不建議預設。

---

## 開發

### 本地跑 syntax check

```bash
python -m compileall -q .
```

### 跑 CI

CI 會自動跑 syntax check + ruff lint (見 `.github/workflows/ci.yml`)。

### 提 PR

歡迎!請先在 issue 討論大方向再開 PR,避免重工。

---

## License

[MIT](LICENSE) - 個人研究用途。商業使用請自行評估合規風險。
