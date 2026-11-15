# 臺南開放資料 ➜ LINE 投放（Gradio · PWA · RWD）

下載臺南市開放資料 CSV → 自訂筆數與排版 → 預覽 → 以 LINE 官方帳號廣播給所有好友。

## 功能
- 使用者在畫面上輸入 **LINE Channel Access Token**（只用於該次傳送，不儲存、不寫入記錄）
- 自訂 **投放則數（筆數）**、顯示欄位、標題欄位、標題／附註、是否顯示時間
- 兩種訊息方式：**合併排版**（依 LINE 字數上限自動分則）／**每筆獨立一則**（最多 5 則）
- 兩種樣式：條列（欄位名稱：內容）／精簡（只列內容）
- LINE 風格即時預覽、靜音推播、勾選確認才能送出
- **PWA**：manifest、service worker、離線頁、可安裝到主畫面
- **RWD**：手機／平板／桌機皆可操作
- 安全：網址 SSRF 防護、10 MB 下載上限、SSL 失敗才退回不驗證

## 本機執行
```bash
pip install -r requirements.txt
python app.py        # http://localhost:7860
```

## 部署到 Render
1. 把整個資料夾推到 GitHub repo。
2. Render → **New ➜ Blueprint**，選該 repo（會讀取 `render.yaml`）。
   或 **New ➜ Web Service**：
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
   - Health Check Path：`/healthz`
3. 部署完成後用 `https://xxx.onrender.com` 開啟（Render 提供 HTTPS，PWA 才能安裝）。

> 免費方案閒置約 15 分鐘會休眠，第一次開啟需等待喚醒。

## 安裝成 App
- Android／電腦 Chrome、Edge：右下角「⬇ 安裝 App」
- iPhone：Safari ➜ 分享 ➜ 加入主畫面

## LINE 注意事項
- Token 取得：LINE Developers ➜ Messaging API channel ➜ *Channel access token (long-lived)*
- `broadcast` 會發給**所有好友**，每則訊息都計入當月額度；合併排版可省額度
- 單次廣播最多 5 則、每則最多 5000 字（程式保留緩衝，用 4500）
- 請勿把 Token 寫死在程式或上傳到 GitHub

## 專案結構
```
app.py            主程式（Gradio + FastAPI + PWA 路由）
requirements.txt
render.yaml       Render Blueprint
pwa/              manifest.json、sw.js、offline.html、圖示
tools/make_icons.py   重新產生圖示（選用）
```
