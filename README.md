# 立法院會議客觀分析

每天自動整理立法院院會、全院委員會與黨團協商的內容，用 AI（Claude）寫成客觀摘要：並列各方主張、整理數字查核清單、對照相關法條，發布成靜態網站。

網站：<https://baize404.github.io/ly-analysis/>

## 運作流程

```
LYAPI（立法院 IVOD 影片 AI 逐字稿／公報發言紀錄）
   ↓ lyanalysis/collect.py   依「公報 > 完整會議逐字稿 > 發言片段逐字稿」選最可靠的來源
全國法規資料庫（指定條文，每週更新）
   ↓ lyanalysis/laws.py
Claude API（撰寫 → 中立性審稿）
   ↓ lyanalysis/analyze.py   被安全機制婉拒時自動改用備用模型
程式檢查（立場用語、未標需查證的法條）
   ↓ lyanalysis/lint.py
靜態網站 docs/ → GitHub Pages
```

GitHub Actions 每天 11:43 與 21:17（臺北時間）執行（`.github/workflows/update.yml`）。公報刊出後會自動改用公報重新分析。

## 第一次設定

1. **加入 API 金鑰**：repo 的 **Settings → Secrets and variables → Actions → New repository secret**，名稱 `ANTHROPIC_API_KEY`，值貼上 Anthropic Console 建立的金鑰。金鑰不要放進程式碼或 commit。
2. **開啟網站**：**Settings → Pages → Build and deployment**，Source 選 **Deploy from a branch**，Branch 選 **main**、資料夾 **/docs**，按 Save。
3. **第一次執行**：**Actions → 更新會議分析 → Run workflow**，「起始日期」填 `2026-11-20` 即可補抓那天之後的會議。

## 出錯時

- Actions 執行有錯誤時會標成紅色 ✗，點進該次執行的 **Summary**，有中文的錯誤原因與處理方式（例如金鑰無效、餘額不足、LYAPI 暫時連不上）。
- 已完成的部分仍會更新到網站；失敗的會議下次排程自動重試。
- 最近一次的錯誤紀錄也存在 `data/last_run.json`。

## 設定（config.json）

| 欄位 | 說明 |
|---|---|
| `meeting_types` | 要分析的會議種類。加入 `"委員會"` 會大幅增加場次與費用 |
| `model` / `fallback_model` | 主要模型與被婉拒時的備用模型 |
| `review_pass` | 是否做第二輪中立性審稿 |
| `max_analyses_per_run` | 每次最多分析幾場，用來控制費用 |
| `lookback_days` | 每次回頭檢查幾天內的會議 |
| `gazette_recheck_days` | 幾天內的會議要持續檢查公報是否刊出 |

## 費用估算

以 Claude Sonnet 5.5（輸入每百萬 tokens 2 美元、輸出 10 美元）計，一場約 7 小時的院會逐字稿約十幾萬字，含審稿大約 1 美元上下。實際用量記錄在各場 `data/meetings/*/meta.json` 的 `analysis.usage`。

## 本機執行

```bash
pip install -r requirements.txt
python -m unittest discover -s tests      # 離線測試
ANTHROPIC_API_KEY=... python run.py --since 2026-11-20
python run.py --only-site                 # 只重新產生網站
```

## 資料來源與授權

- 立法院議事轉播（IVOD）、立法院公報，經 [LYAPI](https://v2.ly.govapi.tw/)（OpenFun，資料 CC BY 4.0）取得。
- 法條取自[全國法規資料庫](https://law.moj.gov.tw/)。
- 分析由 AI 產生，未經人工審核，可能有錯誤，不構成法律意見。本站不隸屬任何政黨或政府機關。
