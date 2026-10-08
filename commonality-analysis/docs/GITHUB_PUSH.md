# 推到 GitHub 的步驟（給 Andrew）

> ⚠️ **憑證一律由你自己輸入**：不要把 Personal Access Token、密碼貼給我或寫進任何檔案。
> 我不需要、也不應該看到它。

## 0. 推送前的檢查（我已在乾淨環境實測）

| 檢查 | 結果 |
|---|---|
| 會被 commit 的檔案 | **26 個、總計約 168 KB**（不含任何資料檔） |
| `data/raw/`（453 MB 資料） | ✅ 已被 `.gitignore` 排除，不會上傳 |
| `.env`（含資料庫帳密） | ✅ 已被 `.gitignore` 排除；只有 `.env.example` 上傳 |
| 金鑰／token 掃描（api_key、secret、token、私鑰、ghp_、sk-、AKIA） | ✅ **無任何命中** |
| `POSTGRES_PASSWORD=ca_dev_pw` | ⚠️ 這是**本機開發用的預設值**（不是機密），保留在 `.env.example` 與 compose 的 fallback；正式環境請改用強密碼 |
| 本機絕對路徑 | ✅ 已清理（`MANIFEST.json` 不再寫入絕對路徑） |
| 大型檔案（GitHub 100 MB 限制） | ✅ 最大檔案 12 KB |

## 1. 在 GitHub 網頁建立空的 repository

1. 登入 <https://github.com>（帳號 `Andrewneteye4343`）
2. 右上角 **`+`** → **New repository**
3. 填寫（**照這樣填，不要變**）：
   - **Repository name**：`commonality-analysis`
   - **Description**：`Semiconductor yield commonality analysis + FDC + time-series forecasting (public PHM 2018 / SECOM data, Docker + PostgreSQL)`
   - **Public**（求職作品集要公開）
   - ❌ **不要**勾 `Add a README file`
   - ❌ **不要**選 `.gitignore` 或 `license` 範本（我們本機已經有了）
4. 按 **Create repository**
5. 建立後畫面會顯示一堆指令，**先不要照它做**（我們照下面的順序，避免 remote 衝突）

## 2. 本機提交（PowerShell，在 `D:\repos\commonality-analysis`）

```powershell
git init
git add .
git commit -m "M1: docker env + data acquisition (PHM 2018 tool 03, SECOM) + lineage"
git tag v0.1
```

確認狀態（應該是「nothing to commit, working tree clean」，且 `data/raw` 沒被追蹤）：

```powershell
git status
git ls-files
```

## 3. 連接遠端並推送

把 `Andrewneteye4343` 換成你的帳號（若是你的帳號就照抄）：

```powershell
git remote add origin https://github.com/Andrewneteye4343/commonality-analysis.git
git branch -M main
git push -u origin main
git push origin v0.1
```

**第一次 push 會發生什麼事**
- Git for Windows 內建的 **Git Credential Manager** 會**彈出瀏覽器視窗**要你登入 GitHub 並授權 → 按同意即可，之後就免再輸入
- 若它反而在終端機問你 `Username` / `Password`：
  - Username 填你的 GitHub 帳號
  - **Password 不能填帳號密碼**（GitHub 已停用），要填 **Personal Access Token**
  - 產生方式：GitHub 網頁 → 右上頭像 → **Settings** → 左側最下方 **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)** → 勾 **`repo`** → 設定到期日（建議 30 天）→ Generate → **複製後立刻貼到終端機**
  - ⚠️ token 只貼進終端機一次，**不要貼到這裡給我、不要寫進任何檔案**
- 想改用 SSH 也可以（`git remote set-url origin git@github.com:Andrewneteye4343/commonality-analysis.git`），需要先把 SSH public key 加到 GitHub。

## 4. 網頁驗證（推送成功的最終確認）

打開 `https://github.com/Andrewneteye4343/commonality-analysis`，確認：

- [ ] 檔案清單有 `docker-compose.yml`、`etl/`、`db/`、`dashboard/`、`README.md`、`M1_CHECKLIST.md`…（共 26 個檔案）
- [ ] **沒有** `data/` 底下的大檔案，**沒有** `.env`
- [ ] `README.md` 在首頁正常顯示（表格、程式碼區塊都沒跑掉）
- [ ] 右側 **Releases / Tags** 看得到 `v0.1`
- [ ] 右上角齒輪 → **Topics** 加上：`semiconductor`、`yield-analysis`、`commonality-analysis`、`fdc`、`docker`、`postgresql`、`time-series-forecasting`、`data-engineering`

（README 中已註明資料來源與「公開學術資料集、已匿名化、未指名任何廠商」，這是誠信紅線，不要拿掉。）

## 5. 之後每個里程碑的推送慣例

```powershell
git add .
git commit -m "M2: genealogy event table + interval join + alignment quality report"
git tag v0.2
git push origin main
git push origin v0.2
```

## 6. 疑難排解

| 症狀 | 處理 |
|---|---|
| `error: remote origin already exists` | `git remote set-url origin https://github.com/Andrewneteye4343/commonality-analysis.git` |
| `failed to push some refs` / `rejected` | 遠端有你本機沒有的 commit：`git pull origin main --rebase` 後再 `git push origin main` |
| 出現 `LF will be replaced by CRLF` 警告 | 正常（`.gitattributes` 已強制 `.py`/`.sql`/`.sh`/`.yml` 用 LF）；若已 commit 才加 `.gitattributes`，執行 `git add --renormalize .` 後再 commit |
| `Support for password authentication was removed` | 需改用 Personal Access Token（見第 3 節）或 SSH key |
| `file is 385 MB; this exceeds GitHub's file size limit` | 表示 `data/raw` 被追蹤了 → `git rm -r --cached data/raw` 後 commit |
| 想把資料也公開分享 | **不要**。改用腳本下載（`etl/download_data.py`），並在 README 標註來源 |
