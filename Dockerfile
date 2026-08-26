FROM node:24-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    DATABASE_URL=sqlite+aiosqlite:////app/data/zhiliutai.db \
    QDRANT_PATH=/app/data/qdrant \
    ARTIFACT_ROOT=/app/data/artifacts
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.2
COPY backend/pyproject.toml backend/uv.lock backend/
RUN uv sync --project backend --locked --no-dev
COPY backend/ backend/
COPY --from=frontend-build /build/frontend/dist frontend/dist
COPY scripts/container-entrypoint.sh scripts/container-entrypoint.sh
RUN chmod +x scripts/container-entrypoint.sh
EXPOSE 8000
VOLUME ["/app/data"]
ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
