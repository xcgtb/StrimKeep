# 前端构建阶段：在镜像里编译 Vue，NAS 主机不需要安装 Node/npm。
# 对 frontend/src 的改动，用 docker compose up -d --build 就能直接生效。
FROM node:22-alpine AS frontend
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --registry="$NPM_REGISTRY"
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    STRIMKEEP_DATA_DIR=/data STRIMKEEP_STRM_DIR=/strm STRIMKEEP_STATIC_DIR=/app/static
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/strimkeep ./strimkeep
# 静态资源只来自上面的前端构建阶段；仓库里遗留的 frontend/dist 不再参与镜像，
# 避免改了 src 却仍在跑旧包。
COPY --from=frontend /build/dist ./static
RUN ["python", "-c", "from pathlib import Path; import re; root=Path('/app/static'); html=(root/'index.html').read_text(); refs=re.findall(r'(?:src|href)=\"(/assets/[^\"]+)\"', html); assert any(ref.endswith('.js') for ref in refs) and all((root/ref.lstrip('/')).is_file() for ref in refs), 'Frontend release assets are incomplete'"]
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health/ready', timeout=3)"]
CMD ["uvicorn", "strimkeep.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "9000", "--workers", "1", "--no-proxy-headers"]
