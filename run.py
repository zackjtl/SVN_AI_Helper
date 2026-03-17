"""
單一啟動入口：以 uvicorn 執行 FastAPI 應用。
- 本機執行：python run.py 或 python -m run
- 打包成單檔時，會先切到執行檔所在目錄，以便讀取同目錄的 .env
"""
import os
import sys


def _chdir_to_app_root() -> None:
    """若以 PyInstaller 打包，執行檔在 dist 內，需切到 bundle 根目錄讀 .env。"""
    if getattr(sys, "frozen", False):
        root = os.path.dirname(sys.executable)
    else:
        root = os.path.dirname(os.path.abspath(__file__))
    # 對於嵌入式 Python，sys.path 可能僅指向 python.exe 目錄，因此手動加入專案根目錄
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)


if __name__ == "__main__":
    _chdir_to_app_root()
    import uvicorn
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8001,
    )
