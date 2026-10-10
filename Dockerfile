FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/xcgtb/StrimKeep"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 应用直接读取 app/version.py；发布标签由 CI 校验。

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY scripts/healthcheck.py ./scripts/healthcheck.py

RUN mkdir -p /data

EXPOSE 8321

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
  CMD python /app/scripts/healthcheck.py || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8321"]
