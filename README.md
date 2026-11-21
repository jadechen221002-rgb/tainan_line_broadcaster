# 台南資料 LINE 投放工具

載入台南市開放資料（CSV）→ 選欄位與排版 → 預覽 → 廣播到 LINE 官方帳號。
Gradio + FastAPI，支援 PWA（可安裝到手機桌面）與 RWD（手機／平板／桌機）。

## 本機執行
```bash
pip install -r requirements.txt
python app.py        # http://localhost:7860
```

## 部署到 Render
1. 把整個資料夾推到 GitHub。
2. Render → New → Web Service（或 Blueprint，會自動讀 `render.yaml`）→ 選這個 repo。
3. 若手動設定：
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`python app.py`
   - Environment：`PYTHON_VERSION=3.11.9`
4. 部署完成後用 HTTPS 網址開啟；手機瀏覽器選單可「加入主畫面」。

## LINE Channel Access Token
- 介面上直接貼入（不會儲存）。
- 或在 Render 設定環境變數 `LINE_CHANNEL_ACCESS_TOKEN` 作為預設值。

## 注意
- 廣播會發給所有好友，並消耗官方帳號的訊息額度。
- 單則文字上限 5000 字，程式會自動分則。
