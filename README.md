# SVN AI Helper

本地SVN操作AI輔助。以自然語言查詢、Checkout、Merge SVN

**所需套件：** Python 3.10+

---

## 功能概覽

- **自然語言下指令**：例如「幫我 checkout ET1289_AP 的 r305」「把 r305 的變更 merge 到這個目錄」。
- **受限的 SVN 操作**：僅允許 `log` / `diff` / `status` / `checkout` / `update` / `merge`，且 URL 限制在設定的 SVN 根底下；不執行 `commit`、`delete`、`move` 等寫入遠端倉庫的指令。
- **工作區內檔案操作**：讀檔、寫檔、列目錄、建立／刪除／複製／移動（皆限制在工作區根目錄內）。
- **checkout 前確認**：執行 checkout 前會先列出遠端 URL 與本地目錄，待使用者同意後才執行。
- **merge 衝突說明**：merge 若發生衝突，不以錯誤呈現，而是提示使用者手動處理並顯示 svn 輸出。
- **多環境支援**：SVN 網址、branch/trunk 目錄命名、本機 svn 路徑、工作區根目錄等皆可透過環境變數設定，方便不同部門佈署。

---

## 環境需求

- **Python 3.10+**（建議 3.11 或 3.12）
- **OpenAI API Key**（或相容 API 的 key）
- 選用：本機已安裝 **Subversion**（例如 TortoiseSVN），以便 AI 代為執行 svn 指令

---

## 快速開始

### 1. 取得專案

```bash
git clone git@github.com:zackjtl/SVN_AI_Helper.git
cd SVN_AI_Helper
```

### 2. 建立虛擬環境並安裝依賴

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / macOS
# source venv/bin/activate

pip install -r requirements.txt
```

### 3. 設定環境變數

複製範例設定檔並編輯：

```bash
# Windows
copy .env.example .env
# Linux / macOS
# cp .env.example .env
```

在 `.env` 中**至少**設定：

- **`OPENAI_API_KEY`**：你的 OpenAI API 金鑰

其餘變數有預設值，可依需要修改（見下方「環境變數」）。

### 4. 啟動服務

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

瀏覽器開啟 **http://127.0.0.1:8000** 即可使用。

更詳細的安裝步驟（含 Python 安裝、虛擬環境說明、常見問題）請見 [安裝與啟用.md](./安裝與啟用.md)。

---

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `OPENAI_API_KEY` | OpenAI API 金鑰（必填） | — |
| `OPENAI_MODEL` | 使用的模型 | `gpt-4.1-mini` |
| `OPENAI_BASE_URL` | API 端點（可接 Azure / Groq / 本地相容 API） | 不設則用 OpenAI 官方 |
| `WORKSPACE_ROOT` | 工作區根目錄（checkout、檔案操作限制於此） | `D:\Projects\AutoMerge` |
| `SVN_BASE_URL` | SVN 伺服器根 URL | `https://svn1.embestor.local/svn/` |
| `SVN_EXE_PATH` | 本機 `svn` 執行檔路徑 | `C:\Program Files\TortoiseSVN\bin\svn.exe` |
| `SVN_ENCODING` | svn 輸出與相關檔案的編碼 | `cp950` |
| `SVN_TIMEOUT_SECONDS` | svn 指令逾時（秒） | `120` |
| `SVN_BRANCH_DIR_NAMES` | branch 目錄名，多個以逗號分隔 | `branches` |
| `SVN_TRUNK_NAME` | trunk 目錄名 | `trunk` |

完整範例與註解見 [.env.example](./.env.example)。

---

## 專案結構

```
svn-ai-helper/
├── app.py              # FastAPI 應用與 AI 對話／工具邏輯
├── requirements.txt   # Python 依賴
├── .env.example        # 環境變數範例（複製為 .env 使用）
├── README.md           # 本說明
└── 安裝與啟用.md       # 詳細安裝與啟用步驟（含無網頁經驗者）
```

---

## 授權與貢獻

本專案供內部或自用；若需對外授權或貢獻方式，可於 repo 內另行補充 LICENSE 與 CONTRIBUTING 說明。
