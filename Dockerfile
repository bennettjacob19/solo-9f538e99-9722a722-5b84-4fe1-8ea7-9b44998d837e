FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x scripts/*.sh

ENV REPO_PATH=/data/sample-repo \
    CHECK_SCRIPT=/app/scripts/sample_check.sh \
    WORKTREE_ROOT=/data/worktrees \
    MAX_CONCURRENT=2 \
    PORT=8000

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
