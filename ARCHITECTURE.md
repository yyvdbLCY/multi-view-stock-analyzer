# 架構設計

> 核心思路：**多視角匯總 → 兩層 LLM 評測 → 附置信度決策**

## 系統流程圖

```
┌──────────────────────────────────────────────────────────────────┐
│  使用者在 Telegram 貼 YouTube URL                                  │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  youtube_client.fetch_video()              │
        │                                            │
        │  ① 嘗試 manual 字幕 (en/en-US/en-GB)        │
        │  ② fallback 到 auto 字幕                    │
        │  ③ (可選) Whisper 音訊轉寫                  │
        │                                            │
        │  → 回傳 VideoInfo (title/channel/transcript) │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  extractor.extract_stocks_from_transcript  │
        │                                            │
        │  ★ Layer 1 LLM (Gemini 2.0 Flash)          │
        │  Prompt: 嚴格 JSON schema                   │
        │  - ticker / company / sentiment             │
        │  - speaker_confidence (high/medium/low)     │
        │  - key_points / price_target / time_horizon │
        │                                            │
        │  → list[StockExtraction]                   │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  storage.save_extraction()                 │
        │  SQLite: extractions table                 │
        │  - video_id, ticker, sentiment, raw_json   │
        │  - UNIQUE 去重 by (video_id, ticker)       │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  aggregator.aggregate_for_ticker()         │
        │                                            │
        │  - 從 storage 撈近 N 天所有提及該 ticker     │
        │  - 補上即時股價 (yfinance)                  │
        │  - 整理成 mentions[] 餵給 Layer 2           │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  evaluator.synthesize_ticker()             │
        │                                            │
        │  ★ Layer 2 LLM (Gemini 2.0 Flash)          │
        │  Prompt: 扮演資深分析師                      │
        │  - 整合 mentions 觀點                      │
        │  - 給出 overall_score (1-10)               │
        │  - 給出 confidence (1-10)  ← 系統置信度     │
        │  - 拆解 confidence_factors 五個維度          │
        │  - key_thesis / risks / takeaway           │
        │                                            │
        │  → 結構化 report dict                       │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  storage.save_report()                     │
        │  - reports table + reports/*.json           │
        │  - 依 run_id 群組                           │
        └────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────┐
        │  main._fmt_report() → Telegram 訊息          │
        │  Markdown 格式化 + 自動切分 3500 字 chunk     │
        └────────────────────────────────────────────┘
```

## 雙層置信度設計

這個系統最重要的設計是「兩種置信度,各司其職」:

| 置信度類型 | 評估對象 | 來源 | 用途 |
|---|---|---|---|
| `speaker_confidence` (high/medium/low) | **YouTuber 本人**的把握度 | Layer 1 從語氣詞判斷 ("definitely" vs "maybe") | 給 Layer 2 當作信號之一,反映「該分析師自己多確定」 |
| `confidence` (1-10) | **整體評測**的可信度 | Layer 2 綜合多項客觀因素 | 給使用者最終決策參考:「這個結論我自己多相信」 |

關鍵洞察: **`overall_score` (看好度) 和 `confidence` (置信度) 是兩個獨立維度**。

- 可能出現「看好度 9 但置信度 3」:某個 YouTuber 非常看好,但只有單一來源、論點空泛
- 也可能出現「看好度 5 但置信度 9」:多個頻道方向一致但都中性,集體共識強

只有當「看好度 + 置信度」兩者都高時,才是真正值得參考的信號。

## Layer 2 Confidence Calibration

`prompts.py` 內的 SYNTHESIS_SYSTEM 明確定義了 confidence 的校準規則:

```
9-10:  5+ 頻道, 強方向共識, 論點全有數據, 48h 內
7-8:   3-4 頻道, 大致同向, 論點尚可, 近期
4-6:   2-3 頻道 或 觀點分歧 或 論點模糊
1-3:   單一來源 或 嚴重分歧 或 純猜測
```

加上硬規則:
- 單一來源 → confidence ≤ 5
- 嚴重方向分歧 → confidence ≤ 4

## 模組依賴圖

```
main.py
  ├── config.py         (env loading)
  ├── youtube_client.py (yt-dlp)
  ├── extractor.py      (Layer 1, Gemini)
  ├── aggregator.py     (聚合邏輯)
  │     ├── storage.py
  │     └── stock_client.py (yfinance)
  ├── evaluator.py      (Layer 2, Gemini)
  └── storage.py        (SQLite)
```

## 資料模型

```sql
videos (1) ─── (∞) extractions (∞) ─── (1) reports
  │                                       │
  video_id  video_id  ticker              ticker, run_id
  url       ticker    sentiment            overall_score
  title               speaker_confidence  confidence
  channel             price_target        summary
  published           key_points_json     full_json
```

每次 `/report TSLA` 或 `/digest` 會產生一個 `run_id`,把同一批報告關聯起來。

## 為什麼用 SQLite?

- MVP 階段單機單進程,沒必要上 PostgreSQL
- zero-config,跟著 repo 走,clone 就跑得起來
- WAL 模式足夠應付 bot 的小流量

需要水平擴展時(例如未來接多個 Telegram 用戶),把 `storage.py` 抽到 Postgres 即可,業務邏輯不用改。

## 為什麼用 Gemini 2.0 Flash?

- 免費額度寬鬆 (15 RPM)
- 對 JSON schema 遵從度高
- 速度快,單支影片通常 5-10 秒完成
- 對字幕拼字錯誤有上下文校正能力

如果想要更好品質:`GEMINI_MODEL_EVAL=gemini-2.5-pro`(成本約 10 倍)。

## 未來可加模組

| 模組 | 價值 | 優先級 |
|---|---|---|
| YouTube Data API 自動掃 6 個頻道新片 | 從手動推送 → 自動消化 | 高 |
| 頻道歷史準確率回測 | 動態權重,不是「聽越多越好」 | 高 |
| 多 LLM 投票 (Gemini + Claude + DeepSeek) | 抗幻覺 | 中 |
| Web Dashboard (Streamlit) | 視覺化歷史曲線 | 中 |
| 推播排程 (cron) | 盤前自動 digest | 中 |
| RAG ticker/公司名辭典 | 字幕校正率提升 | 低 |
| 支援 X / Podcast 輸入 | 多源匯總 | 低 |
