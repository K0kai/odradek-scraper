FROM node:24-bookworm-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY . ./
COPY --from=frontend-build /frontend/dist ./frontend/dist
EXPOSE 8765
CMD ["sh", "-c", "uvicorn web.api_asgi:api --app-dir backend --host 0.0.0.0 --port ${PORT:-8765}"]
