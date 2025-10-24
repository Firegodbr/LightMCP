FROM python:3.12-slim-bookworm
# Install UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
# Set working directory and add project files
WORKDIR /app
ADD . /app
# Sync dependencies and lock them
RUN uv sync --locked
# Expose port (matches .env PORT)
EXPOSE 8000

# Run uvicorn
CMD ["uv", "run", "/src/mcp_server/server.py"]
