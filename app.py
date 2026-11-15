"""臺南開放資料 → LINE 官方帳號廣播（Gradio + FastAPI，PWA / RWD）

功能
- 下載開放資料 CSV（可自訂網址）
- 自訂投放則數、欄位、標題欄、排版樣式，並即時預覽 LINE 訊息
- 使用者自行輸入 LINE Channel Access Token（不儲存、不寫入記錄）
- 以 PWA 方式提供（manifest / service worker / 離線頁），介面採 RWD

本機執行： python app.py            （預設 http://localhost:7860）
Render：   uvicorn app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import html
import io
import ipaddress
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import gradio as gr
import pandas as pd
import requests
import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from linebot.v3.messaging import ApiClient, BroadcastRequest, Configuration, MessagingApi, TextMessage
from linebot.v3.messaging.exceptions import ApiException

BASE_DIR = Path(__file__).resolve().parent
PWA_DIR = BASE_DIR / "pwa"

# --------------------------------------------------------------------------- 常數
DEFAULT_URL = "https://data.tainan.gov.tw/File/ResourceCsvDownload/24b8d897-7971-45f9-8489-b4918ee41ed7"
DEFAULT_HEADER = "📢 臺南市開放資料通知"
DEFAULT_FOOTER = "資料來源：臺南市政府資料開放平臺"

LINE_TEXT_LIMIT = 4500        # LINE 單則文字上限 5000 字，保留緩衝（emoji 可能佔 2 單位）
LINE_MAX_MESSAGES = 5         # 單次 broadcast 最多 5 則訊息
MAX_RECORDS = 100             # 介面可選的最大筆數
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
FIELD_CLIP = 300              # 單一欄位值最長字數
TZ_TW = timezone(timedelta(hours=8))
SEP = "━━━━━━━━━━━━━━"

MODE_MERGE = "合併排版（推薦，省額度）"
MODE_SEPARATE = "每筆獨立一則（最多 5 則）"
STYLE_CARD = "條列（欄位名稱：內容）"
STYLE_COMPACT = "精簡（只列內容）"
NONE_TITLE = "（不使用，以序號為標題）"

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TainanLineBroadcaster/1.0)"}


# --------------------------------------------------------------------------- 資料下載
def _assert_public_url(url: str) -> None:
    """只允許連到公開網路，避免被當成連內網的跳板（SSRF）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("網址必須以 http:// 或 https:// 開頭")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"無法解析網域：{parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("不允許連線到內部網路位址")


def fetch_csv_bytes(url: str) -> tuple[bytes, bool]:
    """下載檔案。回傳 (內容, 是否因憑證問題而略過驗證)。
    先正常驗證 SSL；失敗才退回 verify=False（原程式的行為，政府網站常有憑證鏈問題）。"""
    verify = True
    insecure_used = False
    current = url.strip()
    with requests.Session() as session:
        for _ in range(8):  # 含手動處理的轉址與一次 SSL 重試
            _assert_public_url(current)
            try:
                resp = session.get(current, headers=REQUEST_HEADERS, timeout=30,
                                   verify=verify, allow_redirects=False, stream=True)
            except requests.exceptions.SSLError:
                if not verify:
                    raise
                verify, insecure_used = False, True
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                continue
            if resp.is_redirect and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                resp.close()
                continue
            resp.raise_for_status()
            buf = io.BytesIO()
            for chunk in resp.iter_content(64 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_DOWNLOAD_BYTES:
                    raise ValueError("檔案超過 10 MB，已中止下載")
            return buf.getvalue(), insecure_used
    raise ValueError("轉址次數過多")


def parse_csv(content: bytes) -> pd.DataFrame:
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):  # 繁體中文常見編碼
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str)
        except UnicodeDecodeError as exc:
            last_err = exc
    raise ValueError(f"無法判斷檔案編碼：{last_err}")


def load_data(url: str, drop_na: bool):
    """下載並整理資料 → (df, 預覽表, 標題欄選單, 欄位選單, 狀態訊息)"""
    empty = (None, gr.update(value="", visible=False),
             gr.update(choices=[NONE_TITLE], value=NONE_TITLE), gr.update(choices=[], value=[]))
    url = (url or "").strip()
    if not url:
        return (*empty, "❌ 請輸入資料網址")
    try:
        content, insecure = fetch_csv_bytes(url)
        df = parse_csv(content)
    except requests.exceptions.HTTPError as exc:
        return (*empty, f"❌ 下載失敗：HTTP {exc.response.status_code}")
    except requests.exceptions.RequestException as exc:
        return (*empty, f"❌ 連線失敗：{type(exc).__name__}")
    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        return (*empty, f"❌ 資料處理失敗：{exc}")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    if drop_na:
        df = df.dropna()
    df = df.fillna("").map(lambda v: str(v).strip()).reset_index(drop=True)
    if df.empty:
        return (*empty, "⚠️ 已下載，但沒有可用資料列（可取消「移除含空值的列」再試）")

    cols = list(df.columns)
    default_fields = cols if len(cols) <= 10 else cols[:10]
    note = "（⚠️ 該網站 SSL 憑證驗證失敗，已改用不驗證模式下載）" if insecure else ""
    status = f"✅ 已載入 **{len(df)}** 筆、**{len(cols)}** 個欄位 {note}"
    return (
        df,
        gr.update(value=render_table(df.head(50)), visible=True),
        gr.update(choices=[NONE_TITLE] + cols, value=NONE_TITLE),
        gr.update(choices=cols, value=default_fields),
        status,
    )


# --------------------------------------------------------------------------- 訊息排版
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_record(i: int, row: dict, style: str, title_col: str, fields: list[str]) -> str:
    use_title = title_col != NONE_TITLE
    title_val = _clip(row.get(title_col, ""), 80) if use_title else ""
    others = [f for f in fields if not (use_title and f == title_col)]

    if style == STYLE_COMPACT:
        vals = ([title_val] if title_val else []) + [
            _clip(row[f], 120) for f in others if row.get(f)
        ]
        return f"{i}. " + " ｜ ".join(vals)

    lines = [f"【{i}】{title_val}"]
    lines += [f"▪ {f}：{_clip(row[f], FIELD_CLIP)}" for f in others if row.get(f)]
    return "\n".join(lines)


def build_messages(df, count, mode, style, header, title_col, fields, footer, show_time):
    """把資料排成 LINE 訊息。回傳 (訊息清單, 警告清單, 實際筆數)。"""
    warnings: list[str] = []
    if df is None or len(df) == 0:
        return [], ["尚未載入資料"], 0
    fields = [f for f in (fields or []) if f in df.columns]
    if title_col not in df.columns:
        title_col = NONE_TITLE
    if not fields and title_col == NONE_TITLE:
        return [], ["請至少選擇一個欄位"], 0

    n = max(1, min(int(count or 1), MAX_RECORDS, len(df)))
    blocks = [_format_record(i, r, style, title_col, fields)
              for i, r in enumerate(df.head(n).to_dict("records"), 1)]
    blocks = [_clip(b, LINE_TEXT_LIMIT - 400) for b in blocks]

    title = (header or "").strip() or DEFAULT_HEADER
    stamp = datetime.now(TZ_TW).strftime("%Y-%m-%d %H:%M")
    head_lines = [title] + ([f"🕒 {stamp}"] if show_time else [])
    head_text = "\n".join(head_lines) + f"\n{SEP}"
    footer = (footer or "").strip()

    # ---- 每筆獨立一則
    if mode == MODE_SEPARATE:
        if len(blocks) > LINE_MAX_MESSAGES:
            warnings.append(f"LINE 單次廣播最多 {LINE_MAX_MESSAGES} 則，只投放前 {LINE_MAX_MESSAGES} 筆")
            blocks = blocks[:LINE_MAX_MESSAGES]
        total = len(blocks)
        messages = []
        for i, b in enumerate(blocks, 1):
            msg = f"{title}（{i}/{total}）\n{SEP}\n{b}"
            if i == total and footer:
                msg += f"\n{SEP}\n{footer}"
            messages.append(msg[:LINE_TEXT_LIMIT])
        return messages, warnings, total

    # ---- 合併排版：依字數切成多則
    foot_text = f"{SEP}\n共 {len(blocks)} 筆" + (f"\n{footer}" if footer else "")
    reserve = len(foot_text) + 24           # 頁碼與換行的緩衝
    cont = "（續）"
    groups: list[list[str]] = [[]]
    used = len(head_text) + 2
    for b in blocks:
        if groups[-1] and used + len(b) + 2 + reserve > LINE_TEXT_LIMIT:
            groups.append([])
            used = len(cont) + 2
        groups[-1].append(b)
        used += len(b) + 2

    if len(groups) > LINE_MAX_MESSAGES:
        groups = groups[:LINE_MAX_MESSAGES]
        kept = sum(len(g) for g in groups)
        warnings.append(f"內容超過 LINE 單次廣播上限（{LINE_MAX_MESSAGES} 則），只投放前 {kept} 筆；"
                        "可減少筆數、改用精簡樣式或減少欄位")
    total = sum(len(g) for g in groups)
    foot_text = f"{SEP}\n共 {total} 筆" + (f"\n{footer}" if footer else "")

    messages = []
    for idx, g in enumerate(groups, 1):
        parts = [head_text if idx == 1 else cont, *g]
        if idx == len(groups):
            parts.append(foot_text)
        msg = "\n\n".join(parts)
        if len(groups) > 1:
            msg += f"\n\n（第 {idx}/{len(groups)} 則）"
        messages.append(msg[:LINE_TEXT_LIMIT])
    return messages, warnings, total


def render_table(df: pd.DataFrame) -> str:
    """自製 RWD 表格（外層可橫向捲動）；gr.Dataframe 的虛擬表格會把手機版面撐寬。"""
    body = df.to_html(index=False, border=0, escape=True, classes="data-table")
    return f'<div class="table-label">資料預覽（前 {len(df)} 筆，可左右滑動）</div><div class="table-scroll">{body}</div>'


def render_preview(messages: list[str]) -> str:
    if not messages:
        return '<div class="line-preview"><div class="line-empty">載入資料後，這裡會顯示 LINE 訊息預覽</div></div>'
    parts = []
    for i, m in enumerate(messages, 1):
        parts.append(f'<div class="line-bubble">{html.escape(m)}</div>'
                     f'<div class="line-meta">訊息 {i}/{len(messages)} ・ {len(m)} 字</div>')
    return f'<div class="line-preview">{"".join(parts)}</div>'


def update_preview(df, count, mode, style, header, title_col, fields, footer, show_time):
    messages, warnings, total = build_messages(df, count, mode, style, header, title_col,
                                               fields, footer, show_time)
    if not messages:
        return render_preview([]), f"⚠️ {warnings[0]}" if warnings else ""
    info = f"將投放 **{total}** 筆，共 **{len(messages)}** 則 LINE 訊息"
    if warnings:
        info += "\n\n" + "\n\n".join(f"⚠️ {w}" for w in warnings)
    return render_preview(messages), info


# --------------------------------------------------------------------------- LINE 廣播
def _line_error_text(exc: ApiException) -> str:
    detail = ""
    try:
        import json
        detail = json.loads(exc.body or "{}").get("message", "")
    except Exception:
        pass
    hints = {
        401: "Channel Access Token 無效或已過期，請到 LINE Developers 重新發行",
        403: "此帳號沒有廣播權限（請確認為 Messaging API channel）",
        429: "已超過本月訊息額度或呼叫頻率過高",
    }
    msg = hints.get(exc.status, detail or "LINE 回應錯誤")
    if exc.status == 409:
        msg = "重複的請求，訊息可能已送出"
    return f"❌ 傳送失敗（HTTP {exc.status}）：{msg}" + (f"\n\nLINE 訊息：{detail}" if detail and exc.status in hints else "")


def send_line(token, confirmed, silent, df, count, mode, style, header, title_col,
              fields, footer, show_time):
    if not confirmed:
        return "⚠️ 請先勾選上方的確認事項"
    token = (token or "").strip()
    if not token:
        return "❌ 請輸入 Channel Access Token"
    if not token.isascii() or any(ch.isspace() for ch in token):
        return "❌ Token 格式不正確（應為不含空白的英數字串）"

    messages, warnings, total = build_messages(df, count, mode, style, header, title_col,
                                               fields, footer, show_time)
    if not messages:
        return f"❌ {warnings[0]}" if warnings else "❌ 沒有可傳送的內容"

    try:
        with ApiClient(Configuration(access_token=token)) as client:
            MessagingApi(client).broadcast(
                BroadcastRequest(messages=[TextMessage(text=m) for m in messages],
                                 notification_disabled=bool(silent)),
                x_line_retry_key=str(uuid.uuid4()),
            )
    except ApiException as exc:
        return _line_error_text(exc)
    except (urllib3.exceptions.HTTPError, OSError):
        return "❌ 無法連線到 LINE 伺服器，請檢查網路後再試一次"
    except Exception as exc:  # 不輸出可能含敏感資訊的細節
        return f"❌ 傳送失敗：{type(exc).__name__}"

    done = datetime.now(TZ_TW).strftime("%H:%M:%S")
    extra = "".join(f"\n\n⚠️ {w}" for w in warnings)
    return f"✅ {done} 已廣播 **{total}** 筆（{len(messages)} 則訊息）給所有好友{extra}"


# --------------------------------------------------------------------------- 前端：PWA head / CSS
HEAD_HTML = """
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0e7c66">
<meta name="application-name" content="LINE 投放">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="LINE 投放">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="apple-touch-icon" href="/pwa/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/pwa/favicon.png">
<script>
if ("serviceWorker" in navigator) {
  var _regSW = function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  };
  // Gradio 在頁面載入後才注入 head，load 事件可能已經觸發，所以要先檢查 readyState
  if (document.readyState === "complete") _regSW();
  else window.addEventListener("load", _regSW);
}
// 「安裝 App」按鈕（Chrome / Edge / Android）
var _deferredInstall = null;
window.addEventListener("beforeinstallprompt", function (e) {
  e.preventDefault();
  _deferredInstall = e;
  if (document.getElementById("pwa-install")) return;
  var b = document.createElement("button");
  b.id = "pwa-install";
  b.type = "button";
  b.textContent = "⬇ 安裝 App";
  b.onclick = function () {
    if (!_deferredInstall) return;
    _deferredInstall.prompt();
    _deferredInstall.userChoice.finally(function () {
      _deferredInstall = null;
      b.remove();
    });
  };
  document.body.appendChild(b);
});
window.addEventListener("appinstalled", function () {
  var b = document.getElementById("pwa-install");
  if (b) b.remove();
});
</script>
"""

CSS = """
.gradio-container { max-width: 980px !important; margin: 0 auto !important; }
#app-title h1 { font-size: clamp(1.35rem, 4.2vw, 2rem); margin: .2em 0 .1em; line-height: 1.25; }
#app-title p { margin: 0; opacity: .8; }

/* 避免 iOS 聚焦輸入框時自動放大 */
.gradio-container input, .gradio-container textarea, .gradio-container select { font-size: 16px !important; }
.gradio-container button { min-height: 44px; }

/* 資料預覽表格 */
.table-label { font-size: 13px; opacity: .75; margin-bottom: 6px; }
#data-table-wrap { min-width: 0 !important; max-width: 100%; }
.table-scroll { contain: inline-size; width: 100%; overflow: auto; max-width: 100%; max-height: 300px; border: 1px solid var(--border-color-primary);
  border-radius: 10px; -webkit-overflow-scrolling: touch; }
.table-scroll table { border-collapse: collapse; font-size: 14px; width: max-content; min-width: 100%; }
.table-scroll th, .table-scroll td { padding: 7px 12px; border-bottom: 1px solid var(--border-color-primary);
  text-align: left; white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
.table-scroll th { position: sticky; top: 0; background: var(--background-fill-secondary); z-index: 1; }

/* LINE 風格預覽 */
.line-preview { background: #7b9ac4; border-radius: 14px; padding: 14px 12px; max-height: 520px; overflow-y: auto; }
.line-bubble { background: #a8e06a; color: #111; border-radius: 16px 4px 16px 16px; padding: 10px 14px;
  margin: 0 0 2px auto; width: fit-content; max-width: min(92%, 520px); white-space: pre-wrap;
  word-break: break-word; line-height: 1.55; font-size: 15px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
.line-meta { color: rgba(255,255,255,.85); font-size: 12px; text-align: right; margin: 0 4px 12px; }
.line-empty { color: #fff; text-align: center; padding: 32px 8px; opacity: .9; }

/* 安裝按鈕 */
#pwa-install { position: fixed; right: 16px; bottom: calc(16px + env(safe-area-inset-bottom)); z-index: 9999;
  background: #0e7c66; color: #fff; border: 0; border-radius: 999px; padding: 12px 20px; font-weight: 600;
  box-shadow: 0 6px 20px rgba(0,0,0,.25); cursor: pointer; }

/* 手機版 */
@media (max-width: 640px) {
  .gradio-container { padding: 8px !important; }
  .full-on-mobile button { width: 100% !important; }
  .line-bubble { max-width: 96%; font-size: 14.5px; }
}
/* 以 App 方式開啟（standalone）時微調 */
@media (display-mode: standalone) {
  .gradio-container { padding-top: max(8px, env(safe-area-inset-top)) !important; }
}
"""


# --------------------------------------------------------------------------- Gradio 介面
def build_demo() -> gr.Blocks:
    with gr.Blocks(title="臺南開放資料 LINE 投放", analytics_enabled=False) as demo:
        gr.Markdown("# 📢 臺南開放資料 ➜ LINE 投放\n下載開放資料、排版預覽，再廣播給官方帳號的所有好友。",
                    elem_id="app-title")
        df_state = gr.State(None)

        # 1) 資料來源
        with gr.Accordion("① 資料來源", open=True):
            url = gr.Textbox(label="CSV 網址", value=DEFAULT_URL, lines=2, max_lines=3)
            with gr.Row():
                drop_na = gr.Checkbox(label="移除含空值的列（同原程式 dropna）", value=True, min_width=240)
                load_btn = gr.Button("🔄 下載 / 重新載入資料", variant="secondary", min_width=200,
                                     elem_classes="full-on-mobile")
            data_status = gr.Markdown()
            data_table = gr.HTML(visible=False, elem_id="data-table-wrap")

        # 2) 投放內容
        with gr.Accordion("② 投放設定與排版", open=True):
            with gr.Row():
                count = gr.Number(label="投放則數（筆數）", value=10, minimum=1, maximum=MAX_RECORDS,
                                  precision=0, step=1, min_width=160,
                                  info=f"1～{MAX_RECORDS} 筆；LINE 單次廣播最多 {LINE_MAX_MESSAGES} 則訊息")
                mode = gr.Radio([MODE_MERGE, MODE_SEPARATE], value=MODE_MERGE, label="訊息方式", min_width=260)
            with gr.Row():
                style = gr.Radio([STYLE_CARD, STYLE_COMPACT], value=STYLE_CARD, label="排版樣式", min_width=260)
                title_col = gr.Dropdown([NONE_TITLE], value=NONE_TITLE, label="標題欄位", min_width=200,
                                        info="選一欄當每筆的標題")
            fields = gr.CheckboxGroup([], label="要顯示的欄位")
            with gr.Row():
                header = gr.Textbox(label="訊息標題", value=DEFAULT_HEADER, min_width=240)
                footer = gr.Textbox(label="結尾附註", value=DEFAULT_FOOTER, min_width=240)
            show_time = gr.Checkbox(label="標題下方顯示時間", value=True)

        # 3) 預覽
        with gr.Accordion("③ LINE 訊息預覽", open=True):
            preview_info = gr.Markdown()
            preview_html = gr.HTML(render_preview([]))

        # 4) 發送
        with gr.Accordion("④ 傳送到 LINE", open=True):
            token = gr.Textbox(label="Channel Access Token", type="password", lines=1,
                               placeholder="貼上 LINE Developers 的 Channel access token (long-lived)",
                               info="僅用於這次傳送，不會儲存或寫入記錄。")
            silent = gr.Checkbox(label="靜音推播（好友不會收到通知音）", value=False)
            confirm = gr.Checkbox(label="我了解這會「廣播給所有好友」，且會消耗本月訊息額度", value=False)
            send_btn = gr.Button("🚀 傳送到 LINE", variant="primary", size="lg", interactive=False,
                                 elem_classes="full-on-mobile")
            send_status = gr.Markdown()

        gr.Markdown(
            "<small>📱 安裝成 App：Android / 電腦 Chrome 點右下角「安裝 App」；"
            "iPhone 請用 Safari 點「分享 ➜ 加入主畫面」。</small>"
        )

        # ---- 事件
        cfg = [df_state, count, mode, style, header, title_col, fields, footer, show_time]
        preview_outputs = [preview_html, preview_info]

        load_outputs = [df_state, data_table, title_col, fields, data_status]
        load_btn.click(load_data, [url, drop_na], load_outputs, api_visibility="private") \
            .then(update_preview, cfg, preview_outputs, api_visibility="private")
        demo.load(load_data, [url, drop_na], load_outputs, api_visibility="private") \
            .then(update_preview, cfg, preview_outputs, api_visibility="private")

        # 任何排版設定改變 → 重新預覽
        gr.on(
            triggers=[c.change for c in (count, mode, style, title_col, fields, header, footer, show_time)],
            fn=update_preview, inputs=cfg, outputs=preview_outputs, api_visibility="private",
            show_progress="hidden",
        )

        confirm.change(lambda v: gr.update(interactive=bool(v)), confirm, send_btn,
                       api_visibility="private", show_progress="hidden")
        send_btn.click(
            send_line,
            [token, confirm, silent, df_state, count, mode, style, header, title_col, fields, footer, show_time],
            send_status,
            api_visibility="private",
        )
    return demo


# --------------------------------------------------------------------------- FastAPI（PWA 路由 + 掛載 Gradio）
api = FastAPI(title="tainan-line-broadcaster", docs_url=None, redoc_url=None, openapi_url=None)


@api.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@api.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(PWA_DIR / "manifest.json", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@api.get("/sw.js", include_in_schema=False)
def service_worker():
    # Service worker 必須從根路徑提供，scope 才能涵蓋整個網站
    return FileResponse(PWA_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@api.get("/offline", include_in_schema=False)
def offline_page():
    return FileResponse(PWA_DIR / "offline.html", media_type="text/html")


api.mount("/pwa", StaticFiles(directory=PWA_DIR), name="pwa")

# 必須最後掛載：Gradio 掛在根路徑，會接手所有未被上面路由處理的請求
app = gr.mount_gradio_app(
    api,
    build_demo(),
    path="/",
    theme=gr.themes.Soft(primary_hue="emerald", neutral_hue="slate"),
    css=CSS,
    head=HEAD_HTML,
    favicon_path=str(PWA_DIR / "favicon.png"),
    footer_links=[],
    ssr_mode=False,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
