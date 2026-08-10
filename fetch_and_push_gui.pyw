#!/usr/bin/env python3
"""fetch_and_push_gui.pyw — 家用電腦抓 YouTube 字幕的圖形版工具 (Windows)

用途:
    把 YouTube 影片字幕推給雲端 multi-view-stock-analyzer, 由雲端完成 Gemini
    分析 + Telegram 推送. 本工具提供圖形界面, 不必操作 CMD.

運行方式 (Windows):
    1. 執行  setup_mvsa.bat  (一鍵安裝 yt-dlp + ffmpeg + JavaScript runtime Deno)
       只裝 Python 亦可, 但新 yt-dlp 抓 YouTube 字幕【必須】有 JS runtime
       (deno / node / quirk), 否則會被告知 "<JS runtime> could not be found"。
    2. 設定 INGEST_SECRET (方式見下)
    3. 雙擊本檔 (.pyw 不會跳出黑色指令窗)

設定 INGEST_SECRET (三選一):
    A. 系統環境變數: 控制台→系統→進階系統設定→環境變數→新增
       變數名稱 INGEST_SECRET , 變數值 = 雲端 token
    B. 或在本檔案同資料夾建一個 ingest_settings.env, 內容一行:
        INGEST_SECRET=你的token
    C. 或本視窗內「Token」欄填好 (不建議長期存放)
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

# ============================================================
# 常數與讀取設定
# ============================================================
DEFAULT_INGEST_URL = "http://43.156.50.109:9000/ingest"

def _load_secret_from_env_file():
    """嘗試從本目錄的 ingest_settings.env 讀 INGEST_SECRET (方便不設系統變數)."""
    env_path = Path(__file__).resolve().parent / "ingest_settings.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("INGEST_SECRET="):
                return line.split("=", 1)[1].strip()
    return ""


INGEST_URL = os.getenv("INGEST_URL", DEFAULT_INGEST_URL)
# 環境變數優先, 其次環境檔
INGEST_SECRET = os.getenv("INGEST_SECRET", "").strip() or _load_secret_from_env_file()


def extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def find_ytdlp() -> tuple[str, list[str]]:
    """回傳 (ytdlp 呼叫基底, 參數清單). 優先 python -m yt_dlp, 其次 yt-dlp 執行檔."""
    # 首先試 python 模組 (setup_mvsa.bat 用同一套, 最可靠)
    for py in ("python", "py", sys.executable):
        try:
            r = subprocess.run([py, "-m", "yt_dlp", "--version"],
                               capture_output=True, text=True, timeout=15, check=False)
            if r.returncode == 0:
                # 用 sys.executable 時直接給絕對路徑更穩
                if py == sys.executable:
                    return (sys.executable, ["-m", "yt_dlp"])
                return (py, ["-m", "yt_dlp"])
        except (FileNotFoundError, OSError):
            continue
    # 其次獨立執行檔
    for cand in ("yt-dlp", shutil.which("yt-dlp")):
        if not cand:
            continue
        try:
            subprocess.run([cand, "--version"], capture_output=True,
                           timeout=10, check=False)
            return (cand, [])
        except (FileNotFoundError, OSError):
            continue
    raise RuntimeError("找不到 yt-dlp. 請先執行 setup_mvsa.bat 或 pip install yt-dlp")


def find_js_runtime() -> str | None:
    """回傳 'runtime[:PATH]' 字串 (例: 'deno', 'node', 'deno:C:\\Users\\me\\.deno\\bin\\deno.exe').
    新 yt-dlp 抓 YouTube 必須有 JS runtime; 若 runtime 不在 PATH 只要給絕對路徑
    --js-runtimes deno:C:\\...\\deno.exe 也能用, 故這裡一律回傳可用的最完整形式。"""
    # 優先 shutil.which (已在 PATH 上的最簡單), 回傳純名稱
    for name in ("deno", "node", "qjs"):
        if shutil.which(name):
            return name
    # 其次常見安裝位置但不在 PATH: 用絕對路徑, 交由 yt-dlp 收 deno:PATH
    home = Path.home()
    candidates = [
        ("deno", [
            home / ".deno" / "bin" / ("deno.exe" if os.name == "nt" else "deno"),
        ]),
        ("node", [
            home / ".local" / "bin" / ("node.exe" if os.name == "nt" else "node"),
        ]),
        ("quirk", [
            home / ".local" / "bin" / ("qjs.exe" if os.name == "nt" else "qjs"),
        ]),
    ]
    for name, paths in candidates:
        for p in paths:
            if p.exists():
                return f"{name}:{p}"
    return None


def _vtt_to_text(path) -> str:
    lines = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if (not line or "-->" in line or line.startswith("WEBVTT")
                    or line.startswith("Kind:") or line.startswith("Language:")
                    or line.isdigit()):
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line:
                lines.append(line)
    return "\n".join(lines)


def fetch_transcript(url: str) -> dict:
    ytdlp, pre = find_ytdlp()
    rt = find_js_runtime()
    if not rt:
        raise RuntimeError(
            "找不到 JavaScript runtime (新 yt-dlp 抓 YouTube 必需要 deno/node/quirk)。\n"
            "請先執行 setup_mvsa.bat 安裝, 再重新開啟本視窗。")
    with tempfile.TemporaryDirectory() as td:
        out_tpl = str(Path(td) / "%(id)s.%(ext)s")
        cmd = [ytdlp, *pre, "--skip-download", "--write-auto-sub", "--write-sub",
             "--sub-langs", "en.*", "--sub-format", "vtt", "--write-info-json",
             "--js-runtimes", rt,
             "--no-playlist", "--socket-timeout", "20", "-o", out_tpl, url]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=240,
        )
        subs = sorted(Path(td).glob("*.en*.vtt")) or sorted(Path(td).glob("*.vtt"))
        if proc.returncode != 0 or not subs:
            full_err = (proc.stderr or proc.stdout or "").strip()
            # 過濾掉 Python 棄用警告等無關行, 只留真正有用訊息
            useful = [ln for ln in full_err.splitlines()
                      if not ln.startswith("Deprecated Feature:")]
            err = "\n".join(useful)[-600:] or full_err[-600:]
            if proc.returncode == 0 and not subs:
                raise RuntimeError(
                    "yt-dlp 沒抓到字幕: 該影片可能沒有符合的字幕語言(英文 CC/自動字幕)。\n"
                    f"{err}")
            raise RuntimeError(f"yt-dlp 無法抓字幕: {err}")
        text = _vtt_to_text(subs[0])
        if not text.strip():
            raise RuntimeError("字幕為空 (該片可能無自動字幕)")
        title = channel = published = ""
        try:
            mf = sorted(Path(td).glob("*.info.json"))[0]
            meta = json.loads(mf.read_text(encoding="utf-8"))
            title = meta.get("title", "") or ""
            channel = meta.get("channel", "") or meta.get("uploader", "") or ""
            published = meta.get("upload_date", "") or ""
        except Exception:
            pass
        return {"video_id": extract_video_id(url), "title": title,
                "channel": channel, "published": published,
                "transcript": text, "source": "yt-dlp-home"}


def push(url: str, secret: str, log_cb=None) -> None:
    def log(msg):
        if log_cb:
            log_cb(msg)
        else:
            print(msg)
    if not secret:
        raise RuntimeError("未設定 INGEST_SECRET (Token). 請填 Token 欄或設環境變數.")
    log(f"⟳ 抓字幕: {url}")
    data = fetch_transcript(url)
    log(f"   ✓ 字幕 {len(data['transcript'])} 字元")
    payload = {
        "youtube_url": url, "video_id": data["video_id"],
        "title": data["title"], "channel": data["channel"],
        "published": data["published"], "transcript": data["transcript"],
        "caption_source": data["source"],
    }
    import requests
    resp = requests.post(INGEST_URL, json=payload,
                         headers={"Authorization": f"Bearer {secret}"}, timeout=15)
    if resp.status_code == 200:
        log(f"   ✓ 已送達雲端 ({url})")
    else:
        log(f"   ⚠ 雲端回覆 {resp.status_code}: {resp.text[:200]}")


class IngestGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Multi-View 股票分析 — YouTube 字幕上傳")
        root.geometry("720x560")
        root.minsize(600, 420)

        pad = {"padx": 10, "pady": 6}
        frm = tk.Frame(root)
        frm.pack(fill="x", **pad)

        tk.Label(frm, text="YouTube 網址 (可多個, 一行一個):").grid(row=0, column=0, sticky="w")
        self.txt_urls = tk.Text(frm, height=6, width=60)
        self.txt_urls.grid(row=1, column=0, columnspan=3, sticky="we", pady=4)

        tk.Label(frm, text="Token (INGEST_SECRET):").grid(row=2, column=0, sticky="w")
        self.var_secret = tk.StringVar(value=INGEST_SECRET)
        tk.Entry(frm, textvariable=self.var_secret, show="•", width=50).grid(
            row=2, column=1, sticky="w", pady=2)
        tk.Button(frm, text="從檔案載入", command=self._load_secret).grid(
            row=2, column=2, padx=4)

        btn_row = tk.Frame(root)
        btn_row.pack(fill="x", **pad)
        self.btn_go = tk.Button(btn_row, text="開始處理", command=self._start,
                                bg="#4CAF50", fg="white", padx=20, pady=6)
        self.btn_go.pack(side="left")
        tk.Button(btn_row, text="清空網址", command=lambda: self.txt_urls.delete("1.0", "end")).pack(
            side="left", padx=8)

        self.lbl_status = tk.Label(root, text="", fg="#333")
        self.lbl_status.pack(fill="x", **pad)

        tk.Label(root, text="執行紀錄:").pack(anchor="w", padx=10)
        self.log = scrolledtext.ScrolledText(root, height=14, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.q = queue.Queue()
        self._poll_queue()

    # ---- 工具 ----
    def _load_secret(self):
        p = filedialog.askopenfilename(title="選擇含 INGEST_SECRET 的檔案",
                                       filetypes=[("Environment", "*.env"), ("Text", "*.txt"), ("All", "*.*")])
        if not p:
            return
        for line in Path(p).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("INGEST_SECRET="):
                self.var_secret.set(line.split("=", 1)[1].strip())
                self._log("✓ 已從檔案載入 Token.")
                return
        self._log("⚠ 該檔案內找不到 INGEST_SECRET 行.")

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    # ---- 主流程 ----
    def _start(self):
        raw = self.txt_urls.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        urls = [u for u in urls if extract_video_id(u)]
        if not urls:
            messagebox.showwarning("沒有網址", "請貼入至少一個有效的 YouTube 網址。")
            return
        secret = self.var_secret.get().strip()
        if not secret:
            messagebox.showwarning("沒有 Token", "請填 Token (INGEST_SECRET) 或設定環境變數。")
            return

        self.btn_go.configure(state="disabled", text="處理中…")
        self.lbl_status.configure(text=f"處理 {len(urls)} 支影片…")

        def worker():
            ok = fail = 0
            for u in urls:
                try:
                    push(u, secret, log_cb=lambda m: self.q.put(m))
                    ok += 1
                except Exception as e:
                    self.q.put(f"❌ {u}: {e}")
                    fail += 1
            self.q.put(f"\n==== 完成: 成功 {ok} / 失敗 {fail} ====")
            self.root.after(0, self._finish, f"完成 | 成功 {ok} / 失敗 {fail}")

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, status):
        self.btn_go.configure(state="normal", text="開始處理")
        self.lbl_status.configure(text=status)


def main():
    root = tk.Tk()
    IngestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
