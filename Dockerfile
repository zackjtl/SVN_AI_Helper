# 建置：docker build -t svn-ai-helper .
# 執行：docker run --rm -p 8000:8000 --env-file .env svn-ai-helper
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py run.py .

EXPOSE 8000
# 容器內須監聽 0.0.0.0 才能從 host 連線
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
