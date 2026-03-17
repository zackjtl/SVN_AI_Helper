@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

:: 優先：專案目錄下的嵌入式 Python（免安裝，解壓即用；支援 python-3.11 或 python-3.13）
set EMBED_PY=
if exist "python-3.13\python.exe" set EMBED_PY=python-3.13\python.exe
if exist "python-3.11\python.exe" set EMBED_PY=python-3.11\python.exe
if defined EMBED_PY (
    echo 使用專案內嵌入式 Python...
    rem 若尚未安裝 pip，且包內已有 get-pip.py，先安裝 pip
    "%EMBED_PY%" -m pip --version >nul 2>&1
    if errorlevel 1 (
        if exist "get-pip.py" (
            "%EMBED_PY%" get-pip.py
        ) else if exist "python-3.13\get-pip.py" (
            "%EMBED_PY%" "python-3.13\get-pip.py"
        )
    )
    "%EMBED_PY%" -m pip install -q -r requirements.txt 2>nul
    if errorlevel 1 (
        echo.
        echo [提示] 若為第一次使用，請先啟用 pip：
        echo   1. 用記事本開啟該目錄下的 python3xx._pth，xx 為 312 或 311
        echo   2. 將 import site 那行前面的註解刪掉後存檔
        echo   3. 下載 get-pip.py 後執行: "%EMBED_PY%" get-pip.py
        echo 詳見 安裝與啟用.md 專案內嵌入式 Python 一節。
        pause
        exit /b 1
    )
    "%EMBED_PY%" run.py
    goto :eof
)

:: 若已有虛擬環境，直接用它啟動
if exist "venv\Scripts\python.exe" (
    echo 使用既有虛擬環境啟動...
    "venv\Scripts\python.exe" run.py
    goto :eof
)

:: 找系統 Python（優先 py launcher，其次 python）
set PY=
where py >nul 2>&1 && for /f "tokens=*" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set PY=%%i
if not defined PY where python >nul 2>&1 && for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set PY=%%i
if not defined PY (
    echo 錯誤：找不到 Python。請先安裝 Python 3.10+ 並加入 PATH，或見 安裝與啟用.md 放置專案內嵌入式 Python。
    pause
    exit /b 1
)

echo 偵測到 Python: %PY%
echo 第一次執行：建立虛擬環境並安裝套件，請稍候...
"%PY%" -m venv venv
if errorlevel 1 (
    echo 建立虛擬環境失敗。
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo 安裝套件失敗，請手動執行: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo 啟動服務...
"venv\Scripts\python.exe" run.py
pause
