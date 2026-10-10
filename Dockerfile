FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/xcgtb/StrimKeep"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# CI 自动注入发布/开发版本；本地构建留空时使用源码默认版本。
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}

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
