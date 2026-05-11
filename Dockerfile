FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency files first (layer cache)
COPY pyproject.toml uv.lock* ./

# Install all dependencies (including databricks-sdk and mlflow)
RUN uv sync --no-dev

# Copy source
COPY core/        ./core/
COPY interns/     ./interns/
COPY config/      ./config/
COPY tools/       ./tools/
COPY tests/       ./tests/
COPY dashboard.py ./

# State and workspace dirs are mounted as volumes at runtime
RUN mkdir -p state workspaces

# Default: run the experiment loop
CMD ["uv", "run", "loop"]
