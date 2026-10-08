FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Taipei

# 只裝分析需要的東西：pandas 處理資料、psycopg 連 postgres、
# scipy/statsmodels 供 M3+ 統計檢定、duckdb 供大檔（385 MB 級）掃描不爆記憶體
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /work
CMD ["python", "--version"]
