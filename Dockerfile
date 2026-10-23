# Production image for the C7 live run (supervisor run_live.py -> python -m wallex_agent.shadow --live).
#   docker build --build-arg CODE_VERSION=$(git rev-parse --short HEAD) -t wallex-bot:live .
#   docker compose up -d            (see docker-compose.yml: secrets via .env, state on volumes)
# Secrets are never baked in: .env is excluded by .dockerignore and passed at runtime (env_file / --env-file).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# No .git in the image: the commit that trades is stamped on every record from this build arg.
ARG CODE_VERSION=unknown
ENV WALLEX_CODE_VERSION=${CODE_VERSION}

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p runs data .cache \
    && chown -R bot:bot /app
USER bot

# runs/: live_state.json (resume after restart), logs, status.json · data/: candles · .cache/: LLM USD ledger
VOLUME ["/app/runs", "/app/data", "/app/.cache"]

# docker stop -> SIGTERM -> run_live.py forwards SIGINT to the bot: hooks detached, state saved, NOTHING sold.
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "run_live.py"]
CMD ["--run-id", "live_20261022", "--accounts", "C7", "--llm", "box_sol61,box_sol6,box_sol56", "--end", "2026-10-23 22:50"]
