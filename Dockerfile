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

# Run as a non-root, unprivileged user (hardening: a container-escape or a
# dependency RCE inherits this user's rights, not root's). The app dir --
# including the state/workspaces mount points -- is owned by this user so
# writes to them still work; a host bind-mount with different ownership
# still needs the operator to match the UID or chmod the host dir.
RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/.cache/uv && \
    chown -R appuser:appuser /app
# No home dir (--no-create-home above) -- point uv's cache at the already-
# chowned app tree instead of the default $HOME/.cache, which would 403.
ENV UV_CACHE_DIR=/app/.cache/uv
USER appuser

# Default: read-only health/smoke check (green-gate cannot mutate anything).
# Real invocations pass an explicit command, e.g.
#   docker run <image> uv run workspace-dashboard --workspace <project> ...
# The autonomous mutation loop (`uv run loop`) is intentionally not the
# default — it requires --live --confirm-live-mutation and a human-set
# AUTORESEARCH_ALLOW_LOCAL_MUTATION=1 to write anything, and is not part of
# the platform's launch scope.
CMD ["uv", "run", "green-gate", "--json"]
