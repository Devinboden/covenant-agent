# Hosted demo of the Covenant Agent MCP server (Fly). Build context = repo root (see fly.toml).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COVENANT_HOME=/data/home
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]" "uvicorn>=0.30"
COPY data ./data
COPY deploy/entrypoint.sh /entrypoint.sh
COPY deploy/sync_abstracts.py /app/deploy/sync_abstracts.py
RUN chmod +x /entrypoint.sh
EXPOSE 8080
CMD ["/entrypoint.sh"]
