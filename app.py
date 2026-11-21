"""
台南市開放資料 → LINE 官方帳號廣播工具
Gradio + FastAPI ｜ PWA ｜ RWD ｜ 可部署到 Render
"""
import io
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gradio as gr
import pandas as pd
import requests
import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from linebot.v3.messaging import (
    ApiClient,
    BroadcastRequest,
    Configuration,
    MessagingApi,
    TextMessage,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).parent
PWA_DIR = BASE_DIR / "pwa"

DEFAULT_URL = (
    "https://data.tainan.gov.tw/File/ResourceCsvDownload/"
    "24b8d897-7971-45f9-8489-b4918ee41ed7"
)
LINE_TEXT_LIMIT = 5000       # LINE 單則文字訊息上限
LINE_BATCH_SIZE = 5          # 單次 broadcast 最多 5 則訊息
TW_TZ = timezone(timedelta(hours=8))

STYLES = {
    "卡片條列（推薦）": "card",
    "標題＋重點": "title",
    "精簡單行": "compact",
}


# ----------------------------------------------------------------------------
# 資料載入
# ----------------------------------------------------------------------------
def load_data(url, skip_ssl, drop_na):
    """下載 CSV 並回傳 (DataFrame, 狀態訊息, 欄位選單, 預覽表格)。"""
    url = (url or "").strip()
    if not url:
        return None, "⚠️ 請輸入資料網址。", gr.update(choices=[], value=[]), pd.DataFrame()
    try:
        resp = requests.get(url, verify=not skip_ssl, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8-sig")
        if drop_na:
            df = df.dropna()
        df = df.reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        return (
            None,
            f"❌ 載入失敗：{e}\n\n可嘗試勾選「略過 SSL 驗證」，或確認網址是否正確。",
            gr.update(choices=[], value=[]),
            pd.DataFrame(),
        )
    if df.empty:
        return None, "⚠️ 已下載，但沒有可用資料（可能全被空值篩掉）。", gr.update(choices=[], value=[]), pd.DataFrame()

    cols = list(df.columns)
    return (
        df,
        f"✅ 載入成功：共 {len(df):,} 筆、{len(cols)} 個欄位。",
        gr.update(choices=cols, value=cols[: min(len(cols), 6)]),
        df.head(10),
    )


# ----------------------------------------------------------------------------
# 訊息排版
# ----------------------------------------------------------------------------
def _clean(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().replace("\n", " ")


def _record_block(row, cols, idx, style):
    vals = [(c, _clean(row[c])) for c in cols]
    if style == "compact":
        return f"{idx}. " + " ｜ ".join(v for _, v in vals)
    if style == "title":
        head, rest = vals[0], vals[1:]
        lines = [f"📍 {idx}. {head[1]}"]
        lines += [f"　▸ {c}：{v}" for c, v in rest]
        return "\n".join(lines)
    # card
    lines = [f"📌 第 {idx} 筆", "┈┈┈┈┈┈┈┈"]
    lines += [f"▪ {c}：{v}" for c, v in vals]
    return "\n".join(lines)


def build_messages(df, count, cols, style_label, title, per_msg, show_page, show_time):
    """把 DataFrame 排版成多則 LINE 文字訊息（自動分則、不超過字數上限）。"""
    if df is None or len(df) == 0:
        raise ValueError("請先載入資料。")
    if not cols:
        raise ValueError("請至少勾選一個要顯示的欄位。")

    style = STYLES.get(style_label, "card")
    count = max(1, min(int(count or 1), len(df)))
    per_msg = max(1, int(per_msg or 1))
    sub = df.head(count)

    head_lines = []
    if title and title.strip():
        head_lines.append(f"📊 {title.strip()}")
    meta = f"共 {count} 筆"
    if show_time:
        meta = f"🕒 {datetime.now(TW_TZ):%Y/%m/%d %H:%M}　" + meta
    head_lines.append(meta)
    header = "\n".join(head_lines)

    blocks = [_record_block(r, cols, i, style) for i, (_, r) in enumerate(sub.iterrows(), 1)]
    sep = "\n" if style == "compact" else "\n\n"

    # 預留頁碼空間（例如「\n\n— 12/12 —」）
    reserve = 20 if show_page else 0
    limit = LINE_TEXT_LIMIT - reserve

    msgs, cur, cur_len, cur_n = [], [header], len(header), 0
    for b in blocks:
        b = b[: limit - 1] if len(b) > limit else b
        add = len(sep) + len(b)
        if cur_n >= per_msg or cur_len + add > limit:
            msgs.append(sep.join(cur))
            cur, cur_len, cur_n = [], 0, 0
            add = len(b)
        cur.append(b)
        cur_len += add
        cur_n += 1
    if cur:
        msgs.append(sep.join(cur))

    if show_page and len(msgs) > 1:
        total = len(msgs)
        msgs = [f"{m}\n\n— {i}/{total} —" for i, m in enumerate(msgs, 1)]
    return msgs


def preview(df, count, cols, style_label, title, per_msg, show_page, show_time):
    try:
        msgs = build_messages(df, count, cols, style_label, title, per_msg, show_page, show_time)
    except ValueError as e:
        return f"⚠️ {e}", ""
    parts = [f"【第 {i} 則・{len(m)} 字】\n{m}" for i, m in enumerate(msgs, 1)]
    info = f"將發送 {len(msgs)} 則訊息（每位好友各收到 {len(msgs)} 則，計入官方帳號訊息額度）。"
    return "\n\n════════════════\n\n".join(parts), info


# ----------------------------------------------------------------------------
# LINE 廣播
# ----------------------------------------------------------------------------
def send_broadcast(api_key, df, count, cols, style_label, title, per_msg, show_page, show_time, confirm):
    token = (api_key or "").strip() or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        return "⚠️ 請輸入 Channel Access Token。"
    if not confirm:
        return "⚠️ 請先勾選「我確認要廣播給所有好友」。"
    try:
        msgs = build_messages(df, count, cols, style_label, title, per_msg, show_page, show_time)
    except ValueError as e:
        return f"⚠️ {e}"

    try:
        with ApiClient(Configuration(access_token=token)) as client:
            api = MessagingApi(client)
            for i in range(0, len(msgs), LINE_BATCH_SIZE):
                batch = [TextMessage(text=m) for m in msgs[i : i + LINE_BATCH_SIZE]]
                api.broadcast(BroadcastRequest(messages=batch))
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", "")
        body = getattr(e, "body", "") or str(e)
        return f"❌ 發送失敗 {status}\n{body}"
    return f"✅ 已成功廣播 {len(msgs)} 則訊息給所有好友。"


# ----------------------------------------------------------------------------
# 介面（RWD）
# ----------------------------------------------------------------------------
HEAD = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#06C755">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/pwa/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="LINE 投放">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(console.warn);
  });
}
</script>
"""

CSS = """
.gradio-container {
  max-width: 980px !important;
  margin: 0 auto !important;
  padding-left: max(12px, env(safe-area-inset-left)) !important;
  padding-right: max(12px, env(safe-area-inset-right)) !important;
  padding-bottom: max(16px, env(safe-area-inset-bottom)) !important;
}
#app-title h1 { font-size: clamp(1.35rem, 4.5vw, 2rem); margin-bottom: .2rem; }
#app-title p  { margin-top: 0; opacity: .75; }
.full-btn button, button.full-btn { min-height: 46px; font-size: 1rem; }
#preview-box textarea { font-family: ui-monospace, "Noto Sans Mono CJK TC", monospace; line-height: 1.55; }

@media (max-width: 768px) {
  .gradio-container { padding-left: 8px !important; padding-right: 8px !important; }
  .full-btn, .full-btn button, button.full-btn { width: 100% !important; }
  textarea, input, select { font-size: 16px !important; }  /* 避免 iOS 聚焦時自動放大 */
  #preview-box textarea { font-size: 14px !important; }
}
"""


def build_ui():
    theme = gr.themes.Soft(primary_hue="green", neutral_hue="slate")
    with gr.Blocks(title="台南資料 LINE 投放工具", theme=theme, css=CSS, head=HEAD) as demo:
        gr.Markdown(
            "# 台南資料 LINE 投放工具\n載入開放資料 → 調整排版 → 預覽 → 廣播到 LINE 官方帳號",
            elem_id="app-title",
        )
        df_state = gr.State(None)

        # 1. 資料來源 -------------------------------------------------------
        with gr.Accordion("① 資料來源", open=True):
            url = gr.Textbox(label="CSV 網址", value=DEFAULT_URL, lines=2)
            with gr.Row():
                skip_ssl = gr.Checkbox(label="略過 SSL 驗證", value=True, min_width=160)
                drop_na = gr.Checkbox(label="移除含空值的列", value=True, min_width=160)
            load_btn = gr.Button("載入資料", variant="primary", elem_classes="full-btn")
            load_status = gr.Markdown()
            table = gr.Dataframe(label="資料預覽（前 10 筆）", interactive=False, wrap=True)

        # 2. 排版設定 -------------------------------------------------------
        with gr.Accordion("② 訊息設定與排版", open=True):
            title = gr.Textbox(label="訊息標題", value="台南市資料快報")
            cols = gr.CheckboxGroup(label="要顯示的欄位（順序依資料欄位）", choices=[], value=[])
            with gr.Row():
                count = gr.Number(label="投放筆數", value=10, precision=0, minimum=1, min_width=160)
                per_msg = gr.Number(label="每則訊息放幾筆", value=5, precision=0, minimum=1, min_width=160)
            style = gr.Radio(label="排版樣式", choices=list(STYLES.keys()), value="卡片條列（推薦）")
            with gr.Row():
                show_time = gr.Checkbox(label="標題下顯示時間", value=True, min_width=160)
                show_page = gr.Checkbox(label="多則時顯示頁碼", value=True, min_width=160)
            preview_btn = gr.Button("更新預覽", elem_classes="full-btn")
            preview_info = gr.Markdown()
            preview_box = gr.Textbox(
                label="LINE 訊息預覽", lines=16, max_lines=30, interactive=False,
                show_copy_button=True, elem_id="preview-box",
            )

        # 3. 發送 -----------------------------------------------------------
        with gr.Accordion("③ 發送到 LINE", open=True):
            api_key = gr.Textbox(
                label="Channel Access Token",
                type="password",
                placeholder="貼上 LINE Messaging API 的 Channel Access Token",
                info="只在本次發送使用，不會儲存。也可改用環境變數 LINE_CHANNEL_ACCESS_TOKEN。",
            )
            confirm = gr.Checkbox(label="我確認要廣播給所有好友（會消耗訊息額度）", value=False)
            send_btn = gr.Button("發送廣播", variant="primary", elem_classes="full-btn")
            send_status = gr.Markdown()

        fmt_inputs = [df_state, count, cols, style, title, per_msg, show_page, show_time]

        load_btn.click(load_data, [url, skip_ssl, drop_na], [df_state, load_status, cols, table])
        preview_btn.click(preview, fmt_inputs, [preview_box, preview_info])
        send_btn.click(send_broadcast, [api_key] + fmt_inputs + [confirm], send_status)

        # 開啟頁面時自動載入預設資料
        demo.load(load_data, [url, skip_ssl, drop_na], [df_state, load_status, cols, table])
    return demo


# ----------------------------------------------------------------------------
# FastAPI：PWA 資源 + 掛載 Gradio
# ----------------------------------------------------------------------------
app = FastAPI(title="tainan-line-broadcaster")


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        PWA_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
def service_worker():
    # 放在根路徑，Service Worker 才能控制整個網站
    return FileResponse(
        PWA_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


app.mount("/pwa", StaticFiles(directory=PWA_DIR), name="pwa")
app = gr.mount_gradio_app(app, build_ui(), path="/")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
