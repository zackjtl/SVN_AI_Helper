import os
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import openai
from openai import OpenAI


load_dotenv()  # 載入 .env（OPENAI_*, SVN_*, WORKSPACE_ROOT 等）
# 若設定 OPENAI_BASE_URL，可串接 Azure OpenAI、Groq、本地 OpenAI 相容服務等
_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
client = OpenAI(base_url=_base_url) if _base_url else OpenAI()
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

app = FastAPI(title="SVN AI Helper")

# 簡單的全域對話歷史（單一 session），供 /chat 連續對話用
CHAT_HISTORY: list[dict] = []

# ---------- 可由環境變數覆寫的設定（供不同部門／環境使用）----------
_workspace = os.getenv("WORKSPACE_ROOT", r"D:\Projects\AutoMerge").strip()
DEFAULT_WORKDIR = Path(_workspace)
_svn_base = (os.getenv("SVN_BASE_URL", "https://svn1.embestor.local/svn/").strip() or "https://svn1.embestor.local/svn/").rstrip("/") + "/"
SVN_BASE_PREFIX = _svn_base
SVN_EXE_PATH = os.getenv("SVN_EXE_PATH", r"C:\Program Files\TortoiseSVN\bin\svn.exe").strip() or r"C:\Program Files\TortoiseSVN\bin\svn.exe"
SVN_ENCODING = os.getenv("SVN_ENCODING", "cp950").strip() or "cp950"
SVN_TIMEOUT_SECONDS = int(os.getenv("SVN_TIMEOUT_SECONDS", "120").strip() or "120")
_branch_names = os.getenv("SVN_BRANCH_DIR_NAMES", "branches").strip() or "branches"
SVN_BRANCH_DIR_NAMES = [s.strip() for s in _branch_names.split(",") if s.strip()]
SVN_TRUNK_NAME = (os.getenv("SVN_TRUNK_NAME", "trunk").strip() or "trunk")

# 寫入 CHAT_HISTORY 的 tool 結果上限字元數，避免過大導致下一輪 API 請求 JSON 解析失敗
MAX_TOOL_RESULT_CHARS = 25_000

# follow_up 時只送最近幾則訊息，避免整段歷史過大導致 API 回 400（JSON 無法解析）
MAX_MESSAGES_FOR_FOLLOW_UP = 20

# 記錄最近一段時間 AI 實際在本機執行過的指令，供首頁側邊欄顯示
COMMAND_LOG: list[dict] = []
MAX_COMMAND_LOG = 200


# 定義可給 LLM 呼叫的「工具」(function calling schema)
# 為了避免混淆，目前只暴露一個通用且受限的 svn tool：svn_run_safe
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "svn_run_safe",
            "description": (
                f"在受限範圍內執行 svn 指令。"
                f"只允許 log/diff/status/checkout/update/merge 這幾種 subcommand，"
                f"且遠端 URL 必須位於 {SVN_BASE_PREFIX}[ProjectName] 底下，"
                f"branch 目錄名可能為 {', '.join(SVN_BRANCH_DIR_NAMES)}，trunk 目錄名為 {SVN_TRUNK_NAME}。"
                f"checkout 時必須拉 branch 或 trunk 的下一層整個目錄（完整專案），不可只拉其下的子目錄。"
                f"checkout 時若使用者未指定目錄，可只傳 [URL] 一個參數，後端會自動使用「Repo名稱_時間戳記」作為預設目錄。"
                f"不能用來執行 commit、delete、move 等會改動遠端 repository 的操作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "description": "svn 子指令，例如 log/status/checkout/update/merge",
                        "enum": ["log", "diff", "status", "checkout", "update", "merge"],
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"不含 svn 與 subcommand 的其餘參數。"
                            f"對於 log/diff/status/checkout/update/merge，除非明確是針對目前工作目錄的 working copy 操作，"
                            f"否則最後一個參數必須是完整的 repository URL 或其子路徑（例如 {SVN_BASE_PREFIX}ProjectName/...）。"
                            f"checkout 時可只傳 [URL]，未指定目錄則自動用 Repo名稱_時間戳記；或傳 [URL, 目錄名]。"
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": f"本機工作目錄（相對於 {DEFAULT_WORKDIR}），若省略則用預設根目錄。",
                    },
                },
                "required": ["subcommand", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "讀取工作區內的檔案內容。路徑可為相對（相對於工作區根目錄）或絕對，必須在允許的工作區底下。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "檔案路徑，例如 'BR263/src/main.c' 或相對工作區的路徑。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "寫入或覆寫工作區內的檔案。路徑可為相對（相對於工作區根目錄）或絕對，必須在允許的工作區底下；若檔案不存在會建立。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "檔案路徑。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要寫入的完整內容（文字）。",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "svn_version",
            "description": "查詢目前後端實際使用的 svn client 版本（對應命令列的 `svn --version` 輸出）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作區內某目錄的內容（類似 ls/dir）。路徑可為相對或絕對，須在工作區內。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目錄路徑，例如 '.' 或 'BR263'；留空或 '.' 表示工作區根目錄。",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否遞迴列出子目錄；預設 false 只列一層。",
                        "default": False,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "在工作區內建立目錄（可一次建立多層，類似 mkdir -p）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要建立的目錄路徑。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_path",
            "description": "刪除工作區內的檔案或「空」目錄。目錄若非空請先刪除內容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要刪除的檔案或空目錄路徑。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_path",
            "description": "在工作區內複製檔案或目錄。來源與目的地皆須在工作區內。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "來源檔案或目錄路徑。"},
                    "dst": {"type": "string", "description": "目的地路徑。"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_path",
            "description": "在工作區內移動或重新命名檔案／目錄。來源與目的地皆須在工作區內。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "來源檔案或目錄路徑。"},
                    "dst": {"type": "string", "description": "目的地路徑（移動或新名稱）。"},
                },
                "required": ["src", "dst"],
            },
        },
    },
]


class SvnLogRequest(BaseModel):
    repo_url: str
    revision: int
    workdir: str | None = None  # 若不給就用 DEFAULT_WORKDIR


class ChatRequest(BaseModel):
    message: str
    workdir: str | None = None  # 若不給就用 DEFAULT_WORKDIR


class SvnCheckoutRequest(BaseModel):
    repo_url: str
    local_dir: str  # 可以是相對於 DEFAULT_WORKDIR 的路徑，或絕對路徑


class SvnCheckoutRevisionRequest(BaseModel):
    repo_url: str
    revision: int
    local_dir: str  # 可以是相對於 DEFAULT_WORKDIR 的路徑，或絕對路徑


class SvnUpdateRequest(BaseModel):
    local_dir: str  # 同上


class FileReadRequest(BaseModel):
    path: str  # 檔案路徑（相對或絕對，會限制在 DEFAULT_WORKDIR 底下）


class FileWriteRequest(BaseModel):
    path: str
    content: str


class SvnChangedDirsRequest(BaseModel):
    repo_url: str          # 例如 https://svn1.embestor.local/svn/ET1289_AP
    revision: int          # 例如 305
    project_roots: list[str] | None = None  # 若為 None，預設用從 repo_url 推出的單一 root


class SvnRunSafeRequest(BaseModel):
    subcommand: str                     # log/diff/status/checkout/update/merge 其中之一
    args: list[str]
    working_dir: str | None = None      # 若為 None 則使用 DEFAULT_WORKDIR


def auto_adjust_repo_url_for_revision(repo_url: str, revision: int, cwd: Path) -> str:
    """
    嘗試根據指定 revision 的變更路徑，自動推斷實際所在的 branch 目錄。

    目前策略（以 ET1289_AP 類似專案為主）：
    - 對 repo_url 執行 svn log -v -r {revision}
    - 從 Changed paths 中找出含有 "/{project_name}" 的路徑，例如：
      /branches/NewSTLC - B5x8/ET1289_AP/...
    - 將該路徑截到 "/{project_name}" 為止，組回完整 URL：
      base_root + "/branches/NewSTLC - B5x8/ET1289_AP"
    - 若找不到，就回傳原本的 repo_url。
    """
    try:
        project_name = repo_url.rstrip("/").split("/")[-1]
        if not project_name:
            return repo_url

        lowered = repo_url.lower()
        # 若 URL 已經明確含有任一 branch 目錄名或 trunk 目錄名，就不調整
        if any(f"/{b.lower()}/" in lowered for b in SVN_BRANCH_DIR_NAMES) or f"/{SVN_TRUNK_NAME.lower()}/" in lowered:
            return repo_url

        log_text = run_svn_command(
            ["log", "-v", "-r", str(revision), repo_url],
            cwd=cwd,
        )

        base_root = repo_url.rstrip("/")
        split_token = "/" + project_name
        if split_token in base_root:
            base_root = base_root.rsplit(split_token, 1)[0]

        for line in log_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            path = parts[1]
            if not path.startswith("/"):
                continue
            if f"/{project_name}" not in path:
                continue
            idx = path.index(f"/{project_name}")
            branch_subpath = path[: idx + len(f"/{project_name}")]
            adjusted = (base_root + branch_subpath).replace("//", "/")
            if base_root.startswith("http://") or base_root.startswith("https://"):
                adjusted = adjusted.replace("http:/", "http://").replace("https:/", "https://")
            return adjusted
    except Exception:
        return repo_url

    return repo_url


def _svn_checkout_to_local(repo_url: str, local_dir: str, revision: int | None = None) -> dict:
    """
    內部共用的 checkout helper。

    repo_url: 如 https://svn1.embestor.local/svn/ET1289_AP[/branches/...]
    local_dir: 目錄（可相對於 DEFAULT_WORKDIR）
    revision: 若給定，會以 URL@rev 方式 checkout 該版本，且會試圖自動推斷 branch 目錄。
    """
    # 若指定了 revision，且 URL 目前看起來是專案 root，就自動根據 log 推斷 branch 路徑
    if revision is not None:
        repo_url = auto_adjust_repo_url_for_revision(repo_url, revision, DEFAULT_WORKDIR)

    local_path = Path(local_dir)
    if not local_path.is_absolute():
        local_path = DEFAULT_WORKDIR / local_path
    local_path = ensure_in_workspace(local_path)

    parent = local_path.parent
    target_name = local_path.name
    parent.mkdir(parents=True, exist_ok=True)

    checkout_url = f"{repo_url}@{revision}" if revision is not None else repo_url
    output = run_svn_command(
        ["checkout", checkout_url, target_name],
        cwd=parent,
    )

    return {
        "repo_url": repo_url,
        "local_dir": str(local_path),
        "revision": revision,
        "svn_output": output,
    }


def _run_svn_command_raw(args: list[str], cwd: Path) -> tuple[str, str, int]:
    """執行 svn，回傳 (stdout, stderr, returncode)，並記錄到 COMMAND_LOG。不拋錯。"""
    default_ssl_flags = [
        "--non-interactive",
        "--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,other",
    ]
    cmd_list = [SVN_EXE_PATH, *default_ssl_flags, *args]
    COMMAND_LOG.append({"cmd": " ".join(cmd_list), "cwd": str(cwd)})
    if len(COMMAND_LOG) > MAX_COMMAND_LOG:
        del COMMAND_LOG[0 : len(COMMAND_LOG) - MAX_COMMAND_LOG]

    try:
        result = subprocess.run(
            cmd_list,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding=SVN_ENCODING,
            errors="replace",
            timeout=SVN_TIMEOUT_SECONDS,
        )
        return (result.stdout, result.stderr, result.returncode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"執行 svn 失敗: {e}")


def run_svn_command(args: list[str], cwd: Path) -> str:
    """在指定目錄下執行 svn，回傳 stdout（失敗丟 500），並記錄到 COMMAND_LOG。"""
    stdout, stderr, returncode = _run_svn_command_raw(args, cwd)
    if returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"svn 失敗，退出碼 {returncode}，stderr:\n{stderr}",
        )
    return stdout


def ensure_in_workspace(path: Path) -> Path:
    """確保路徑在 DEFAULT_WORKDIR 底下，避免誤動到其他地方。"""
    path = path.resolve()
    if not str(path).lower().startswith(str(DEFAULT_WORKDIR.resolve()).lower()):
        raise HTTPException(
            status_code=400,
            detail=f"路徑超出允許範圍：{path}（工作區根目錄為 {DEFAULT_WORKDIR}）",
        )
    return path


def resolve_workdir_path(path_str: str) -> Path:
    """將工具參數中的路徑轉成工作區內的 Path（相對路徑以 DEFAULT_WORKDIR 為根）。"""
    path_str = (path_str or ".").strip() or "."
    p = Path(path_str)
    if not p.is_absolute():
        p = DEFAULT_WORKDIR / p
    return ensure_in_workspace(p.resolve())


def _trim_messages_safe(history: list[dict], max_count: int) -> list[dict]:
    """截斷為最多 max_count 則，且不切開「assistant(tool_calls) + 對應 tool 訊息」區塊，避免 API 400。"""
    if len(history) <= max_count:
        out = list(history)
    else:
        system = history[0]
        rest = history[1:]
        start = max(0, len(rest) - (max_count - 1))
        # 若截斷後第一則是非 system 的 tool，會缺前面的 assistant，API 會 400；往前包到該區塊開頭
        while start < len(rest) and rest[start].get("role") == "tool":
            start -= 1
            if start < 0:
                start = 0
                break
        out = [system] + rest[start:]
    # 結尾不能是「只有 assistant(tool_calls)、沒有對應 tool 訊息」
    while len(out) > 1 and out[-1].get("role") == "assistant" and out[-1].get("tool_calls"):
        out.pop()
    # 從頭檢查：若有 assistant(tool_calls)，緊接必須有 n 則 tool；否則只刪掉該不完整區塊（不刪到後面 user）
    i = 1
    while i < len(out):
        m = out[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            n = len(m["tool_calls"])
            k = 0
            while i + 1 + k < len(out) and out[i + 1 + k].get("role") == "tool":
                k += 1
            if k < n:
                # 區塊不完整：只刪此 assistant 與後面已存在的 tool 訊息
                del out[i : i + 1 + k]
                continue
            i += 1 + n
        else:
            i += 1
    return out


def _tool_result_for_history(result: dict) -> str:
    """將 tool 回傳的 result 轉成可安全寫入 CHAT_HISTORY 的字串（截斷過長、移除會破壞 JSON 的控制字元）。"""
    raw = json.dumps(result, ensure_ascii=False)
    # 移除或替換會導致 JSON 解析問題的控制字元（保留 \n \r \t）
    sanitized = "".join(
        c if c in "\n\r\t" or ord(c) >= 32 else " "
        for c in raw
    )
    if len(sanitized) <= MAX_TOOL_RESULT_CHARS:
        return sanitized
    return sanitized[: MAX_TOOL_RESULT_CHARS] + "\n...(內容過長已截斷，僅保留前 {} 字元)".format(MAX_TOOL_RESULT_CHARS)


def ensure_api_key():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="請在環境變數或 .env 設定 OPENAI_API_KEY")


ALLOWED_SVN_SUBCOMMANDS: set[str] = {"log", "diff", "status", "checkout", "update", "merge"}


def _derive_checkout_default_name(url: str) -> str:
    """
    從 checkout URL 推導預設目錄名稱的「前半段」：Repo名稱_Branch名稱。
    例如：
    - https://.../svn/ET1289_AP/branches/BR263 -> ET1289_AP_BR263
    - https://.../svn/ET1289_AP/trunk          -> ET1289_AP_trunk

    實際目錄名稱會再加上 _時間戳，由呼叫端組成：Repo_Branch_YYYYMMDD_HHMMSS。
    """
    url = (url or "").strip().rstrip("/")
    if not url.startswith(SVN_BASE_PREFIX):
        return "repo"
    path_part = url[len(SVN_BASE_PREFIX) :].lstrip("/")
    if not path_part:
        return "repo"
    parts = path_part.split("/")
    project = parts[0] if parts else "repo"

    branch = "repo"
    if len(parts) >= 3 and parts[1] in SVN_BRANCH_DIR_NAMES:
        branch = parts[2] or parts[1]
    elif len(parts) >= 2 and parts[1] == SVN_TRUNK_NAME:
        branch = SVN_TRUNK_NAME
    elif len(parts) >= 2:
        # 其它情況，取最後一段當作「類似 branch 的名稱」
        branch = parts[-1] or project
    else:
        branch = project

    return f"{project}_{branch}"


def handle_svn_run_safe(
    subcommand: str,
    args: list[str],
    working_dir: str | None = None,
) -> dict:
    """
    在受限範圍內執行 svn 指令的共用實作。
    - 只允許 ALLOWED_SVN_SUBCOMMANDS 中的子指令。
    - 所有遠端 URL 參數必須位於 SVN_BASE_PREFIX 底下，且格式為 SVN_BASE_PREFIX + ProjectName[/...]
    - 本機工作目錄必須位於 DEFAULT_WORKDIR 底下。
    """
    subcommand = subcommand.strip().lower()
    if subcommand not in ALLOWED_SVN_SUBCOMMANDS:
        raise HTTPException(status_code=400, detail=f"不被允許的 svn 子指令：{subcommand}")

    # 檢查參數中的 URL 是否符合前綴，並統計是否至少帶了一個 URL（避免模型漏掉 repository URL）
    seen_url = False
    for a in args:
        a_stripped = a.strip()
        if a_stripped.startswith("http://") or a_stripped.startswith("https://"):
            seen_url = True
            if not a_stripped.startswith(SVN_BASE_PREFIX):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"URL 超出允許範圍：{a_stripped}（必須位於 {SVN_BASE_PREFIX}[ProjectName] 底下）"
                    ),
                )

    # 除了純針對已存在 working copy 的 status/update/merge 之外，log/diff/checkout 若完全沒帶 URL，通常是模型漏了 repository
    if subcommand in {"log", "diff", "checkout"} and not seen_url:
        raise HTTPException(
            status_code=400,
            detail=(
                f"執行 svn {subcommand} 時缺少 repository URL，請在 args 中加入完整的 URL，"
                f"例如 {SVN_BASE_PREFIX}ProjectName/{SVN_BRANCH_DIR_NAMES[0] if SVN_BRANCH_DIR_NAMES else 'branches'}/BR263 或其子路徑。"
            ),
        )

    # 決定工作目錄
    if working_dir:
        cwd_path = Path(working_dir)
        if not cwd_path.is_absolute():
            cwd_path = DEFAULT_WORKDIR / cwd_path
        cwd = ensure_in_workspace(cwd_path)
    else:
        cwd = DEFAULT_WORKDIR

    # checkout 若只給 URL、未指定目錄，預設為 [Repo名稱]_[時間戳記]
    if subcommand == "checkout" and len(args) == 1:
        url = args[0].strip()
        name = _derive_checkout_default_name(url)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir = f"{name}_{ts}"
        args = list(args) + [default_dir]

    if subcommand == "merge":
        stdout, stderr, returncode = _run_svn_command_raw([subcommand, *args], cwd=cwd)
        full_output = (stdout + "\n" + stderr).strip() if stderr else stdout
        if returncode != 0:
            return {
                "subcommand": subcommand,
                "args": args,
                "working_dir": str(cwd),
                "svn_output": full_output,
                "svn_stdout": stdout,
                "svn_stderr": stderr,
                "returncode": returncode,
                "conflicts_need_manual_resolution": True,
                "message": "merge 執行後發生衝突（或非零退出），請您手動處理衝突後再 resolve/commit。以下為 svn 的完整輸出。",
            }
        return {
            "subcommand": subcommand,
            "args": args,
            "working_dir": str(cwd),
            "svn_output": stdout,
        }

    output = run_svn_command([subcommand, *args], cwd=cwd)
    return {
        "subcommand": subcommand,
        "args": args,
        "working_dir": str(cwd),
        "svn_output": output,
    }


def list_changed_dirs_for_revision(
    repo_url: str,
    revision: int,
    project_roots: list[str] | None = None,
) -> dict:
    """
    只查詢某個 revision 的變動目錄，不做 checkout。

    repo_url: 例如 https://svn1.embestor.local/svn/ET1289_AP
    revision: 例如 305
    project_roots: 在 repository 裡此專案的根路徑清單，例如：
        ["/ET1289_AP", "/branches/BR206/ET1289_AP"]
      若為 None，預設只用從 repo_url 推出來的那一個。
    """
    log_text = run_svn_command(
        ["log", "-v", "-r", str(revision), repo_url],
        cwd=DEFAULT_WORKDIR,
    )

    # 在目前系統假設下：
    # - repo_url（例如 https://.../svn/ET1289_AP）被視為「獨立專案的根」。
    # - Changed paths 會列出此專案底下的絕對路徑（以 / 開頭）。
    # 因此預設專案 root 視為 "/"，也就是「整個這個 repo 的根」。
    repo_root = repo_url.rstrip("/")

    if not project_roots:
        # "/" 代表「整個 repo」，即所有 Changed paths 都視為屬於本專案。
        project_roots = ["/"]

    changed_dirs: set[str] = set()
    matched_paths: list[str] = []

    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if " " not in stripped:
            continue
        action, path = stripped.split(" ", 1)
        path = path.strip()
        if not path.startswith("/"):
            continue

        matched_root = None
        for root in project_roots:
            # "/" 代表整個 repo
            root_clean = root.rstrip("/") or "/"
            if root_clean == "/":
                matched_root = "/"
                break
            if path == root_clean or path.startswith(root_clean + "/"):
                matched_root = root_clean
                break

        if not matched_root:
            continue

        matched_paths.append(path)

        dir_path = path
        if not dir_path.endswith("/") and "/" in dir_path:
            dir_path = dir_path.rsplit("/", 1)[0]
        dir_path = dir_path.rstrip("/")
        if not dir_path:
            continue

        # 轉成「相對於專案 root」的路徑。
        if matched_root == "/":
            rel = dir_path.lstrip("/")
        else:
            rel = dir_path[len(matched_root):].lstrip("/")
        changed_dirs.add(rel or ".")

    return {
        "repo_root": repo_root,
        "project_roots": project_roots,
        "revision": revision,
        "raw_log": log_text,
        "matched_paths": matched_paths,
        "relative_changed_dirs": sorted(changed_dirs),
    }


@app.post("/svn-log-report")
def svn_log_report(body: SvnLogRequest):
    """查詢指定 revision 的 svn log，請 AI 產生 HTML 報告並回傳。"""
    workdir = Path(body.workdir) if body.workdir else DEFAULT_WORKDIR

    # 1) 取得 svn log -v
    log_text = run_svn_command(
        ["log", "-v", "-r", str(body.revision), body.repo_url],
        cwd=workdir,
    )

    # 避免一次把超大 log 丟給 LLM，導致超過 token/分鐘或 context 限制
    MAX_LOG_CHARS = 40000
    if len(log_text) > MAX_LOG_CHARS:
        log_text = log_text[:MAX_LOG_CHARS]

    # 2) 呼叫 LLM，請它把 log 轉成 HTML 報告
    prompt = f"""
你是一個熟悉 SVN 的工程師，請將下面這段 svn log 轉成一份 HTML 報告。

需求：
- 使用 UTF-8 編碼
- 最上方列出 repo URL 與 revision
- 用表格整理：
  - Revision
  - Author
  - Date
  - Commit message（多行可合併）
  - Changed paths（用 <code>...</code> 顯示，每行一筆）
- 最後簡短總結這次修改在做什麼（自然語言描述即可）

以下是 svn log 內容（原始文字，若過長僅保留前段）：

{log_text}
"""

    ensure_api_key()

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": "你是一位資深軟體工程師，會產生乾淨的 HTML 報告。"},
                {"role": "user", "content": prompt},
            ],
        )
    except openai.RateLimitError as e:
        raise HTTPException(status_code=429, detail=f"OpenAI RateLimitError: {e}")

    html = response.choices[0].message.content

    # 將 HTML 直接寫成檔案，避免出現 \n 轉義字元
    output_dir = Path(__file__).resolve().parent
    output_path = output_dir / f"svn_log_r{body.revision}.html"
    try:
        output_path.write_text(html, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入報告檔失敗: {e}")

    # 回傳檔案路徑與 HTML 內容（JSON 裡的 html 仍然會有跳行轉義，但檔案是乾淨的）
    return {
        "repo_url": body.repo_url,
        "revision": body.revision,
        "report_path": str(output_path),
        "html": html,
    }


def handle_svn_log_report(repo_url: str, revision: int) -> dict:
    """tool: svn_log_report 的後端實作。"""
    body = SvnLogRequest(repo_url=repo_url, revision=revision)
    result = svn_log_report(body)
    return {"report_path": result["report_path"]}


def handle_svn_checkout_and_modify(
    repo_url: str,
    revision: int,
    local_dir: str,
    file_path: str,
    comment: str,
) -> dict:
    """tool: svn_checkout_and_modify 的後端實作。"""
    # 1) 自動調整 repo_url（根據 rXXX 找出實際 branch directory）
    repo_url_adj = auto_adjust_repo_url_for_revision(repo_url, revision, DEFAULT_WORKDIR)

    # 2) checkout 到本地
    co_result = _svn_checkout_to_local(
        repo_url=repo_url_adj,
        local_dir=local_dir,
        revision=revision,
    )

    checkout_path = Path(co_result["local_dir"])
    target_file = ensure_in_workspace(checkout_path / file_path)

    if not target_file.exists():
        raise HTTPException(status_code=404, detail=f"要修改的檔案不存在：{target_file}")

    original = target_file.read_text(encoding=SVN_ENCODING, errors="ignore")

    line = f"// {comment}\n"
    new_content = line + original

    target_file.write_text(new_content, encoding=SVN_ENCODING)

    return {
        "checked_out_from": repo_url_adj,
        "local_dir": str(checkout_path),
        "modified_file": str(target_file),
    }


def handle_svn_checkout_revision(
    repo_url: str,
    revision: int,
    local_dir: str,
) -> dict:
    """
    tool: svn_checkout_revision 的後端實作。

    會根據指定 revision 自動推斷實際 branch 目錄，
    然後將該 revision 的整個專案 checkout 到本機目錄。
    """
    # 先根據 revision 自動調整 repo_url（推斷實際 branch）
    repo_url_adj = auto_adjust_repo_url_for_revision(repo_url, revision, DEFAULT_WORKDIR)

    co_result = _svn_checkout_to_local(
        repo_url=repo_url_adj,
        local_dir=local_dir,
        revision=revision,
    )

    return co_result


def handle_svn_checkout_changed_dirs_for_revision(
    repo_url: str,
    revision: int,
    local_dir: str,
) -> dict:
    """
    tool: svn_checkout_changed_dirs_for_revision 的後端實作。

    流程：
    1. 先呼叫 list_changed_dirs_for_revision 取得此 repo 在該 revision 下的變動目錄（相對路徑）。
    2. 再依相對路徑組出完整遠端 URL，逐一 checkout 到本機指定根目錄。
    """
    local_root = Path(local_dir)
    if not local_root.is_absolute():
        local_root = DEFAULT_WORKDIR / local_root
    local_root = ensure_in_workspace(local_root)
    local_root.mkdir(parents=True, exist_ok=True)

    # 1) 先取得變動目錄（相對於 repo_url 根）
    info = list_changed_dirs_for_revision(
        repo_url=repo_url,
        revision=revision,
        project_roots=None,  # 預設視為整個 repo
    )
    relative_dirs: list[str] = info.get("relative_changed_dirs", [])

    # 若沒有任何相對路徑，直接回傳結果（不做 checkout）
    if not relative_dirs:
        return {
            "repo_url": repo_url,
            "revision": revision,
            "local_root": str(local_root),
            "changed_dirs": [],
            "checked_out": [],
        }

    # 只 checkout「有變動的專案根目錄」：
    # 規則：若路徑為 [branch_dir]/[BranchName]/... 或 [trunk]/...，則專案根取前兩段或第一段
    grouped_dirs: set[str] = set()
    for rel in relative_dirs:
        if rel in ("", "."):
            grouped_dirs.add(".")
            continue
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] in SVN_BRANCH_DIR_NAMES:
            key = f"{parts[0]}/{parts[1]}"
        else:
            key = parts[0]
        grouped_dirs.add(key or ".")

    base_url = repo_url.rstrip("/")
    checked_out: list[dict] = []

    for rel_dir in sorted(grouped_dirs):
        # "." 代表專案根目錄本身
        if rel_dir in ("", "."):
            remote_dir_url = base_url
            subpath = ""
        else:
            remote_dir_url = f"{base_url}/{rel_dir}"
            subpath = rel_dir.replace("/", "\\")

        target_dir = local_root / subpath if subpath else local_root
        target_parent = target_dir.parent
        target_parent.mkdir(parents=True, exist_ok=True)

        checkout_url = f"{remote_dir_url}@{revision}"
        svn_output = run_svn_command(
            ["checkout", checkout_url, target_dir.name],
            cwd=target_parent,
        )

        checked_out.append(
            {
                "remote_dir_url": remote_dir_url,
                "local_dir": str(target_dir),
                "svn_output": svn_output,
            }
        )

    return {
        "repo_url": repo_url,
        "revision": revision,
        "local_root": str(local_root),
        "changed_dirs": sorted(grouped_dirs),
        "checked_out": checked_out,
    }


@app.post("/chat")
def chat(body: ChatRequest):
    """
    自然語言對話入口：內部使用 OpenAI tools (function calling) 來選擇要執行的動作。
    會累積 CHAT_HISTORY，讓模型能銜接上一輪對話。
    """
    ensure_api_key()

    global CHAT_HISTORY

    _branch_dirs = "、".join(SVN_BRANCH_DIR_NAMES) or "branches"
    _base = SVN_BASE_PREFIX.rstrip("/")
    _workdir = str(DEFAULT_WORKDIR)
    system_content = (
        "你是一個懂 SVN 的助理。\n"
        "\n"
        "【工具總覽】\n"
        "你可使用的 tools：\n"
        "1) svn_run_safe：在受限範圍內執行 svn log/diff/status/checkout/update/merge。\n"
        "2) file_read：讀取工作區內的檔案內容。\n"
        "3) file_write：寫入或覆寫工作區內的檔案。\n"
        "4) list_dir：列出目錄內容（類似 ls/dir，可選遞迴）。\n"
        "5) mkdir：建立目錄（可多層）。\n"
        "6) remove_path：刪除檔案或空目錄。\n"
        "7) copy_path：複製檔案或目錄。\n"
        "8) move_path：移動或重新命名檔案／目錄。\n"
        "9) svn_version：查詢目前後端實際使用的 svn client 版本（執行 svn --version）。\n"
        "以上路徑相關操作皆限制在工作區內。當使用者要求讀取、修改、編輯、列出、建立、刪除、複製、移動檔案或目錄時，請用對應工具完成；"
        "當使用者問「你用的是哪個 svn 版本」時，請用 svn_version 工具查詢並以自然語言說明。\n"
        "\n"
        f"【SVN 結構與基本規則】\n"
        f"SVN 伺服器根為 {_base} ，實際專案的 repository root 形式為 {SVN_BASE_PREFIX}[ProjectName]，"
        f"例如 ET1289_AP 這個專案就是 {SVN_BASE_PREFIX}ET1289_AP。{_branch_dirs} 的下一層目錄代表某個分支上的完整專案根，"
        f"{SVN_TRUNK_NAME} 底下同樣是一個完整專案根。所有的 svn 操作都會有特定的 branch 或 trunk 對象，trunk 在 {SVN_TRUNK_NAME} 目錄，branch 則在 {_branch_dirs} 目錄底下，"
        f"這些子目錄是專案根目錄，在 checkout 時必須拉出整個專案根目錄，不能只拉出特定某層的子目錄。\n"
        "\n"
        "【關於 revision 的關鍵規則（務必遵守）】\n"
        f"1) 當使用者給你某個 revision 編號，要你『checkout / 拉下 / 匯出 / 套用』該版時：\n"
        f"   (a) 你必須先用 svn_run_safe 觸發 svn log -v -r N 等指令，查出該 revision 實際位於哪一個 {_branch_dirs}/[BranchName] 或 {SVN_TRUNK_NAME} 底下，以及對應的 [ProjectName]。\n"
        f"   (b) 找到後，你一律以該 branch 或 trunk 的『完整專案根』作為後續 checkout/merge 的對象。\n"
        "   (c) 你在任何與這個 revision 有關的 svn 指令中，都要記得加上 -r N（或 -c N 對應 merge）來鎖定該版本。\n"
        f"2) 在 checkout 時，絕不能只 checkout {_branch_dirs}/[BranchName] 或 {SVN_TRUNK_NAME} 底下的局部子目錄；必須 checkout『該 branch 或 trunk 的下一層完整專案目錄』，那才是完整專案。\n"
        f"3) 若使用者要求的 revision 其變更路徑在更深層，你仍然必須以該完整專案根為 checkout URL，不能只 checkout 變更所在的子目錄。\n"
        "\n"
        "【checkout 前後的溝通規則】\n"
        "在你打算執行任何會在本機進行 checkout 的 svn 指令（例如 svn checkout、或以 checkout 為目的的複合流程）之前：\n"
        f"1) 先以自然語言向使用者說明你『打算』checkout 的遠端目錄 URL 以及目標本地目錄完整路徑（例如：我要執行 svn checkout -r 305 [遠端URL] 到 {_workdir}/ET1289_AP_BR263_20240704_135124）。\n"
        "2) 明確詢問使用者是否同意執行這個 checkout（例如：『請確認是否要執行上述 checkout？(是/否)』），並等待使用者回答。\n"
        "3) 只有在使用者明確表達同意後，才呼叫 svn_run_safe 實際執行 checkout；若使用者不同意或要求修改，則依使用者的新指示重新規劃，不得逕自執行 checkout。\n"
        "4) checkout 完成後，請再用一則簡短訊息明確告知本次實際使用的遠端目錄 URL 與本地目錄完整路徑（這兩個資訊要一起出現），讓使用者一眼就能看出結果放在哪裡、是從哪一個遠端目錄拉下來的。\n"
        "\n"
        "【預設本地目錄命名規則】\n"
        "當使用者要求你 checkout 某個 revision，但『沒有指定本地目錄』時，你必須遵守以下規則：\n"
        "1) 目錄名稱格式固定為：Repo名稱_Branch目錄名_時間戳，例如：ET1288_AP_2370_20240704_135124。\n"
        "2) 在呼叫 svn_run_safe 的 checkout 時，若使用者未指定目錄，你只在 args 中傳 [URL] 一個參數，讓後端依上述規則自動決定實際目錄名稱；若使用者有指定目錄，則在 args 中傳 [URL, 目錄名]。\n"
        "\n"
        "【允許與禁止的操作】\n"
        "你可以且應該用 svn_run_safe 執行 checkout、update、merge，這些會變更使用者本地工作目錄的檔案，這是允許且預期的；僅不得對遠端 repository 執行 commit、delete、move 等寫入操作。\n"
        "\n"
        "【回覆風格】\n"
        "若你有使用 svn_run_safe 或檔案相關工具，在回覆使用者時請用自然語言清楚說明你做了哪些事（例如執行了哪些 svn 指令、目標路徑、是否有使用 -r N、結果摘要等），不要只回傳工具結果。"
        "若因目前提供的工具不足而無法執行使用者的指令時，請明確回覆無法執行，並在說明中指出需要什麼指令或工具才能達成（例如需要 commit 權限、需要某個未提供的 API、或需在別處執行某指令）。\n"
        "\n"
        "【merge 衝突時的說明方式】\n"
        "當 svn_run_safe 執行 merge 後，若工具回傳結果中含有 conflicts_need_manual_resolution 或 message 提到「請您手動處理衝突」時，不要將結果呈現為「錯誤」或「ERROR」。"
        "請改為以中性語氣說明：此次 merge 已執行，但產生了衝突，需要使用者手動處理（例如用 TortoiseSVN 或指令列 resolve）；並將工具回傳的 svn_output（或 svn_stdout/svn_stderr）完整顯示給使用者，方便他依 svn 的提示進行後續操作。\n"
        "\n"
        "【簡短示範】\n"
        "範例：\n"
        f"User：『幫我把某專案的 r305 拉下來。』\n"
        f"你應先用 svn_run_safe 執行類似：svn log -v -r 305 {SVN_BASE_PREFIX}[ProjectName]，"
        f"找到實際變更路徑所屬的 {_branch_dirs}/[BranchName]/[ProjectName] 或 {SVN_TRUNK_NAME}/[ProjectName]，"
        f"再用 svn_run_safe 執行 svn checkout -r 305 [完整專案根 URL] [依規則產生的本地目錄名稱]，並在回覆中說明你查到的是哪個 branch/trunk，以及實際使用的 checkout 指令與目錄名稱。\n"
    )

    if body.message.strip() == "/reset":
        CHAT_HISTORY.clear()
        return {"reply": "對話歷史已清除，之後從新的一輪開始。"}

    if not CHAT_HISTORY:
        CHAT_HISTORY.append({"role": "system", "content": system_content})

    CHAT_HISTORY.append({"role": "user", "content": body.message})
    messages = _trim_messages_safe(CHAT_HISTORY, MAX_MESSAGES_FOR_FOLLOW_UP)

    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )

    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []
    if not tool_calls:
        # 純文字回答：寫回 CHAT_HISTORY，下一輪才能銜接
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        CHAT_HISTORY.append(assistant_msg)
        return {"reply": msg.content}

    def run_one_tool(tool_name: str, tool_args: dict):
        """執行單一 tool，回傳 result dict。"""
        if tool_name == "svn_log_report":
            return handle_svn_log_report(**tool_args)
        if tool_name == "svn_checkout_and_modify":
            return handle_svn_checkout_and_modify(**tool_args)
        if tool_name == "svn_checkout_revision":
            return handle_svn_checkout_revision(**tool_args)
        if tool_name == "svn_checkout_changed_dirs_for_revision":
            return handle_svn_checkout_changed_dirs_for_revision(**tool_args)
        if tool_name == "svn_run_safe":
            return handle_svn_run_safe(**tool_args)
        if tool_name == "file_read":
            path = Path(tool_args.get("path", ""))
            if not path.is_absolute():
                path = DEFAULT_WORKDIR / path
            path = ensure_in_workspace(path)
            if not path.exists():
                return {"error": f"檔案不存在：{path}"}
            if not path.is_file():
                return {"error": f"不是檔案：{path}"}
            return {"path": str(path), "content": path.read_text(encoding="utf-8", errors="ignore")}
        if tool_name == "file_write":
            path = Path(tool_args.get("path", ""))
            content = tool_args.get("content", "")
            if not path.is_absolute():
                path = DEFAULT_WORKDIR / path
            path = ensure_in_workspace(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"path": str(path), "size": len(content)}
        if tool_name == "list_dir":
            path = resolve_workdir_path(tool_args.get("path", "."))
            if not path.exists():
                return {"error": f"路徑不存在：{path}"}
            if not path.is_dir():
                return {"error": f"不是目錄：{path}"}
            recursive = bool(tool_args.get("recursive", False))
            entries = []
            if recursive:
                for p in sorted(path.rglob("*")):
                    rel = p.relative_to(path)
                    entries.append({"path": str(rel), "name": p.name, "type": "dir" if p.is_dir() else "file"})
            else:
                for p in sorted(path.iterdir()):
                    entries.append({"name": p.name, "type": "dir" if p.is_dir() else "file"})
            return {"path": str(path), "entries": entries, "recursive": recursive}
        if tool_name == "mkdir":
            path = resolve_workdir_path(tool_args.get("path", ""))
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path)}
        if tool_name == "remove_path":
            path = resolve_workdir_path(tool_args.get("path", ""))
            if not path.exists():
                return {"error": f"路徑不存在：{path}"}
            if path.is_file():
                path.unlink()
                return {"removed": str(path), "type": "file"}
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError as e:
                    return {"error": f"目錄非空或無法刪除：{path}（{e}）"}
                return {"removed": str(path), "type": "dir"}
            return {"error": f"無法刪除：{path}"}
        if tool_name == "copy_path":
            src = resolve_workdir_path(tool_args.get("src", ""))
            dst = resolve_workdir_path(tool_args.get("dst", ""))
            if not src.exists():
                return {"error": f"來源不存在：{src}"}
            if src.is_file():
                shutil.copy2(src, dst)
                return {"src": str(src), "dst": str(dst), "type": "file"}
            shutil.copytree(src, dst)
            return {"src": str(src), "dst": str(dst), "type": "dir"}
        if tool_name == "move_path":
            src = resolve_workdir_path(tool_args.get("src", ""))
            dst = resolve_workdir_path(tool_args.get("dst", ""))
            if not src.exists():
                return {"error": f"來源不存在：{src}"}
            shutil.move(str(src), str(dst))
            return {"src": str(src), "dst": str(dst)}
        if tool_name == "svn_version":
            # 使用 DEFAULT_WORKDIR 作為執行目錄呼叫 svn --version
            output = run_svn_command(["--version"], cwd=DEFAULT_WORKDIR)
            return {"svn_version": output}
        raise HTTPException(status_code=400, detail=f"未知的 tool: {tool_name}")

    # 先執行所有 tool 並收集結果，任一失敗則不寫入歷史（並還原本輪 user），避免留下不完整的 assistant+tool 導致下一輪 400
    assistant_msg = {
        "role": "assistant",
        "content": msg.content or None,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ],
    }

    first_tool_name = None
    first_tool_args = None
    first_result = None
    any_summary_tool = False
    tool_results: list[tuple] = []  # (tc, result)

    try:
        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"工具參數不是合法 JSON：{tc.function.arguments}")
            if first_tool_name is None:
                first_tool_name = tool_name
                first_tool_args = tool_args
            result = run_one_tool(tool_name, tool_args)
            if first_result is None:
                first_result = result
        if tool_name in (
            "svn_run_safe", "file_read", "file_write",
            "list_dir", "mkdir", "remove_path", "copy_path", "move_path",
            "svn_version",
        ):
            any_summary_tool = True
            tool_results.append((tc, result))
    except Exception:
        # 本輪已 append user，但尚未寫入 assistant/tool；還原 CHAT_HISTORY 並重新拋出
        CHAT_HISTORY.pop()
        raise

    # 全部成功後才寫入歷史，確保每個 tool_call_id 都有對應的 tool 訊息（寫入前截斷過長內容，避免下一輪 API 請求 JSON 失敗）
    CHAT_HISTORY.append(assistant_msg)
    for tc, result in tool_results:
        CHAT_HISTORY.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _tool_result_for_history(result),
            }
        )

    # 若用了 svn_run_safe 或檔案讀寫，再請模型用自然語言說明做了哪些事（不送整段歷史，改送「system + 工具結果」避免 assistant+tool_calls 區塊導致 API 400）
    if any_summary_tool:
        summary_prompt = (
            "請用一兩句話總結剛才執行的工具結果，給使用者看。\n\n"
            f"工具：{first_tool_name}\n"
            f"結果：\n{_tool_result_for_history(first_result)}"
        )
        follow_up_messages = [
            CHAT_HISTORY[0],
            {"role": "user", "content": summary_prompt},
        ]
        try:
            follow_up = client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=follow_up_messages,
                tools=TOOLS,
                tool_choice="none",
            )
            summary_msg = follow_up.choices[0].message
            if summary_msg.content:
                CHAT_HISTORY.append({"role": "assistant", "content": summary_msg.content})
                return {
                    "reply": summary_msg.content,
                    "tool_used": first_tool_name,
                    "tool_args": first_tool_args,
                    "result": first_result,
                }
        except openai.BadRequestError as e:
            # payload 過大或格式問題時不讓整次請求 500，改回傳 tool 結果摘要
            err_msg = getattr(e, "message", str(e))
            fallback_reply = f"（自然語言總結暫時無法產生：{err_msg[:200]}）\n\n已執行：{first_tool_name}，結果已寫入對話歷史。"
            CHAT_HISTORY.append({"role": "assistant", "content": fallback_reply})
            return {
                "reply": fallback_reply,
                "tool_used": first_tool_name,
                "tool_args": first_tool_args,
                "result": first_result,
            }

    return {
        "tool_used": first_tool_name,
        "tool_args": first_tool_args,
        "result": first_result,
    }


@app.post("/svn-checkout")
def svn_checkout(body: SvnCheckoutRequest):
    """
    從遠端 SVN repository checkout 到本地目錄。

    範例：repo_url 須在 SVN_BASE_URL 底下；local_dir 可為絕對路徑或相對路徑（相對 WORKSPACE_ROOT）。
    """
    # 處理本地目錄路徑與父目錄
    local_path = Path(body.local_dir)
    if not local_path.is_absolute():
        local_path = DEFAULT_WORKDIR / local_path
    local_path = ensure_in_workspace(local_path)

    parent = local_path.parent
    target_name = local_path.name

    # 確保父目錄存在
    parent.mkdir(parents=True, exist_ok=True)

    # svn checkout URL PATH 需要在父目錄下執行
    output = run_svn_command(
        ["checkout", body.repo_url, target_name],
        cwd=parent,
    )

    return {
        "repo_url": body.repo_url,
        "local_dir": str(local_path),
        "svn_output": output,
    }


@app.post("/svn-update")
def svn_update(body: SvnUpdateRequest):
    """
    在指定本地工作目錄下執行 svn update。
    """
    local_path = Path(body.local_dir)
    if not local_path.is_absolute():
        local_path = DEFAULT_WORKDIR / local_path
    local_path = ensure_in_workspace(local_path)

    if not local_path.exists():
        raise HTTPException(status_code=400, detail=f"目錄不存在：{local_path}")

    output = run_svn_command(["update"], cwd=local_path)
    return {
        "local_dir": str(local_path),
        "svn_output": output,
    }


@app.post("/file-read")
def file_read(body: FileReadRequest):
    """
    讀取檔案內容（限制在 DEFAULT_WORKDIR 底下）。
    """
    file_path = Path(body.path)
    if not file_path.is_absolute():
        file_path = DEFAULT_WORKDIR / file_path
    file_path = ensure_in_workspace(file_path)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"檔案不存在：{file_path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"不是檔案：{file_path}")

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取檔案失敗：{e}")

    return {
        "path": str(file_path),
        "content": content,
    }


@app.post("/file-write")
def file_write(body: FileWriteRequest):
    """
    覆寫檔案內容（限制在 DEFAULT_WORKDIR 底下）。
    若檔案不存在會自動建立其父目錄與檔案。
    """
    file_path = Path(body.path)
    if not file_path.is_absolute():
        file_path = DEFAULT_WORKDIR / file_path
    file_path = ensure_in_workspace(file_path)

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(body.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入檔案失敗：{e}")

    return {
        "path": str(file_path),
        "size": len(body.content),
    }


@app.post("/svn-changed-dirs")
def svn_changed_dirs(body: SvnChangedDirsRequest):
    """
    查詢某個 repository / 專案在指定 revision 底下「有哪些目錄有變更」。
    只查詢，不做 checkout。
    """
    result = list_changed_dirs_for_revision(
        repo_url=body.repo_url,
        revision=body.revision,
        project_roots=body.project_roots,
    )
    return result


@app.post("/svn-run-safe")
def svn_run_safe(body: SvnRunSafeRequest):
    """
    HTTP 入口：在受限範圍內執行 svn 指令。
    建議只在除錯時直接呼叫；在 /chat 中應透過工具調用。
    """
    result = handle_svn_run_safe(
        subcommand=body.subcommand,
        args=body.args,
        working_dir=body.working_dir,
    )
    return result


@app.get("/command-log")
def get_command_log():
    """
    取得最近執行過的 svn 指令紀錄，供首頁側邊欄顯示。
    """
    return {"commands": COMMAND_LOG}


@app.get("/", response_class=HTMLResponse)
def index():
    """簡單的人性化首頁：左側聊天，右側顯示 AI 執行的指令紀錄。"""
    # 每次載入／重新整理首頁（含 F5）時清空右側指令列
    COMMAND_LOG.clear()
    return """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <title>SVN AI Helper</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 0;
      background: #f3f4f6;
      color: #1f2933;
    }
    h1 {
      font-size: 22px;
      margin: 0 0 4px 0;
    }
    button {
      padding: 8px 18px;
      border-radius: 999px;
      border: 1px solid #2563eb;
      background: #2563eb;
      color: #fff;
      font-size: 14px;
      cursor: pointer;
    }
    button:hover {
      background: #1d4ed8;
      border-color: #1d4ed8;
    }
    button:active {
      background: #1e40af;
      border-color: #1e40af;
    }
    .layout {
      display: flex;
      height: 100vh;
    }
    .main-pane {
      flex: 7; /* 約 70% 寬度 */
      padding: 20px 20px 16px 24px;
      box-sizing: border-box;
      overflow: hidden;
      border-right: 1px solid rgba(148, 163, 184, 0.35);
      display: flex;
      flex-direction: column;
    }
    .sidebar {
      flex: 3; /* 約 30% 寬度，比原本更寬一些 */
      min-width: 340px;
      padding: 16px 20px;
      box-sizing: border-box;
      background: #f8fafc;
      border-left: 1px solid rgba(148, 163, 184, 0.25);
      overflow: auto;
    }
    .result {
      margin-top: 12px;
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .chat-container {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    #responseArea {
      flex: 1;
      min-height: 120px;
      padding: 14px 12px;
      background: #f9fafb;
      border: 1px solid rgba(203, 213, 225, 0.9);
      border-radius: 16px;
      line-height: 1.5;
      overflow-y: auto;
    }
    #responseArea pre { white-space: pre-wrap; word-break: break-all; margin: 0.5em 0; }
    #responseArea .tool-summary { margin-top: 8px; }
    .chat-message {
      margin-bottom: 10px;
      display: flex;
    }
    .chat-message.user {
      justify-content: flex-end;
    }
    .chat-message.assistant {
      justify-content: flex-start;
    }
    .chat-bubble {
      max-width: 78%;
      padding: 8px 12px;
      border-radius: 16px;
      font-size: 14px;
      line-height: 1.6;
      word-break: break-word;
    }
    .chat-bubble.user {
      background: #3b82f6;
      color: #f9fafb;
      border-top-right-radius: 4px;
    }
    .chat-bubble.assistant {
      background-color: #ffffff;
      color: #111827;
      border: 1px solid rgba(209, 213, 219, 0.9);
      border-top-left-radius: 4px;
    }
    .input-area {
      margin-top: 10px;
    }
    .input-row {
      display: flex;
      gap: 8px;
      align-items: flex-end;
    }
    .input-row textarea {
      flex: 1;
      resize: vertical;
      min-height: 56px;
      max-height: 160px;
      width: 100%;
      font-family: inherit;
      font-size: 14px;
      padding: 8px 10px;
      border-radius: 12px;
      border: 1px solid rgba(209, 213, 219, 0.9);
      outline: none;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
      background: rgba(255, 255, 255, 0.96);
    }
    .input-row textarea:focus {
      border-color: #4f46e5;
      box-shadow: 0 0 0 1px rgba(79, 70, 229, 0.35);
    }
    #cmdLog {
      font-family: Consolas, monospace;
      font-size: 12px;
      padding: 0;
      max-height: calc(100vh - 140px);
      overflow-y: auto;
    }
    .cmd-entry {
      border-radius: 6px;
      padding: 6px 8px;
      margin-bottom: 6px;
      background-color: #ffffff;
      border: 1px solid rgba(226, 232, 240, 0.9);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
    }
    .cmd-entry:last-child {
      margin-bottom: 0;
    }
    .cmd-cwd {
      font-size: 11px;
      color: #6b7280;
      margin-bottom: 2px;
    }
    .cmd-text {
      word-break: break-all;
      color: #111827;
    }
    .sidebar-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 4px;
    }
    .sidebar h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0.01em;
      color: #111827;
    }
    .cmd-ghost-btn {
      opacity: 0;
      border: none;
      background: transparent;
      padding: 4px 8px;
      font-size: 12px;
      color: #6b7280;
      cursor: pointer;
      border-radius: 6px;
      transition: opacity 0.15s ease;
    }
    .sidebar-header:hover .cmd-ghost-btn,
    .cmd-ghost-btn:hover,
    .cmd-ghost-btn:focus {
      opacity: 1;
    }
    .cmd-ghost-btn:hover {
      color: #111827;
      background: rgba(0, 0, 0, 0.06);
    }
    .hint {
      font-size: 12px;
      color: #6b7280;
      margin-bottom: 8px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: rgba(37, 99, 235, 0.08);
      color: #1d4ed8;
      margin-left: 4px;
    }
  </style>
</head>
<body>
  <div class="layout">
    <div class="main-pane">
      <h1>SVN AI Helper <span class="pill">Beta</span></h1>
      <p style="margin: 2px 0 10px 0; font-size: 13px; color: #4b5563;">
        讓 AI 幫你查 log、checkout、比對、套用 revision。
      </p>
      <ul>
        <li>「請幫我產生 ET1289_AP 這個 repository 的 r263 變更報告。」</li>
        <li>「幫我看 r305 改了什麼，生成 HTML 報告。」</li>
      </ul>

      <div class="result">
        <div class="chat-container">
          <div id="responseArea" data-empty="true">(尚無對話)</div>
        </div>
        <div class="input-area">
          <div class="input-row">
            <textarea id="prompt" placeholder="在這裡輸入指令..."></textarea>
            <button onclick="sendPrompt()">送出</button>
          </div>
        </div>
      </div>
    </div>
    <div class="sidebar">
      <div class="sidebar-header">
        <h2>AI 執行指令</h2>
        <button type="button" class="cmd-ghost-btn" onclick="refreshCommandLog()" title="重新整理">重新整理</button>
      </div>
      <div class="hint">顯示最近由 AI 在本機執行的 svn 指令（read-only）。</div>
      <div id="cmdLog">(尚無指令)</div>
    </div>
  </div>

  <script>
    function appendMessage(role, html) {
      const area = document.getElementById('responseArea');
      if (area.dataset.empty === 'true') {
        area.innerHTML = '';
        area.dataset.empty = 'false';
      }
      const wrapper = document.createElement('div');
      wrapper.className = 'chat-message ' + (role === 'user' ? 'user' : 'assistant');
      const bubble = document.createElement('div');
      bubble.className = 'chat-bubble ' + (role === 'user' ? 'user' : 'assistant');
      bubble.innerHTML = html;
      wrapper.appendChild(bubble);
      area.appendChild(wrapper);
      area.scrollTop = area.scrollHeight;
    }
    function appendUserMessage(text) {
      const html = escapeHtml(text).replace(/\\n/g, '<br>');
      appendMessage('user', html);
    }
    function appendAssistantMessageFromText(txt) {
      const html = escapeHtml(txt).replace(/\\n/g, '<br>');
      appendMessage('assistant', html);
    }
    function toolResultToHtml(data) {
      if (!data.tool_used || !data.result) return '';
      const r = data.result;
      let html = '<p><strong>已執行：</strong> ' + data.tool_used + '</p>';
      if (r.svn_output) {
        html += '<p><strong>輸出：</strong></p><pre>' + escapeHtml(r.svn_output) + '</pre>';
      }
      if (r.local_root) html += '<p><strong>本機目錄：</strong> ' + escapeHtml(r.local_root) + '</p>';
      if (r.changed_dirs && r.changed_dirs.length) {
        html += '<p><strong>變動目錄：</strong> ' + escapeHtml(r.changed_dirs.join(', ')) + '</p>';
      }
      if (r.checked_out && r.checked_out.length) {
        html += '<p><strong>已 checkout：</strong> ' + r.checked_out.length + ' 個目錄</p>';
      }
      if (r.report_path) html += '<p><strong>報告檔案：</strong> ' + escapeHtml(r.report_path) + '</p>';
      return html || '<p>完成。</p>';
    }
    function escapeHtml(s) {
      if (s == null) return '';
      const div = document.createElement('div');
      div.textContent = s;
      return div.innerHTML;
    }
    async function sendPrompt() {
      const textarea = document.getElementById('prompt');
      const prompt = textarea.value.trim();
      if (!prompt) {
        alert('請先輸入內容');
        return;
      }
      appendUserMessage(prompt);
      textarea.value = '';
      try {
        const resp = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: prompt })
        });
        if (!resp.ok) {
          const text = await resp.text();
          appendMessage('assistant', '<span style=\"color:red\">發生錯誤：' + escapeHtml(resp.status + ' ' + text) + '</span>');
          return;
        }
        const data = await resp.json();
        if (data.reply) {
          appendMessage('assistant', (data.reply || '').replace(/\\n/g, '<br>'));
        } else if (data.tool_used) {
          appendMessage('assistant', toolResultToHtml(data));
        } else {
          appendMessage('assistant', '<p>' + escapeHtml(JSON.stringify(data)) + '</p>');
        }
      } catch (e) {
        appendMessage('assistant', '<span style=\"color:red\">呼叫失敗：' + escapeHtml(String(e)) + '</span>');
      }
    }

    async function refreshCommandLog() {
      try {
        const resp = await fetch('/command-log');
        if (!resp.ok) return;
        const data = await resp.json();
        const cmds = Array.isArray(data.commands) ? data.commands : [];
        if (!cmds.length) {
          document.getElementById('cmdLog').textContent = '(尚無指令)';
          return;
        }
        const html = cmds.map(c => {
          const cwd = escapeHtml(c.cwd || '');
          const cmd = escapeHtml(c.cmd || '');
          return '<div class=\"cmd-entry\">'
               + (cwd ? '<div class=\"cmd-cwd\">[' + cwd + ']</div>' : '')
               + '<div class=\"cmd-text\">' + cmd + '</div>'
               + '</div>';
        }).join('');
        document.getElementById('cmdLog').innerHTML = html;
      } catch (e) {
        // ignore errors
      }
    }

    // 進入頁面後，每 3 秒更新一次指令紀錄
    refreshCommandLog();
    setInterval(refreshCommandLog, 3000);
  </script>
</body>
</html>
"""

