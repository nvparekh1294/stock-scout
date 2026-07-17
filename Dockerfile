# Telegram relay + scheduled research loops (one worker service).
# Secrets arrive as environment variables at runtime; NOTHING secret is baked
# into the image (.env and all private data directories are excluded via
# .dockerignore).
FROM python:3.12-slim

# Production stdout is block-buffered, so print() logs would be invisible in
# the platform's live log stream until the buffer flushed. Unbuffer both
# streams so logs appear as they happen.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scout/ scout/
COPY prompts/ prompts/

# Non-secret settings. The glob always matches the tracked config.example.yml,
# and also picks up a config.yml if you committed one in your fork. load_config()
# prefers config.yml and falls back to config.example.yml, so the container boots
# either way (the example fails closed: empty SEC contact, no tax jurisdiction,
# scheduled loops off). Secrets never live here — they arrive as env vars.
COPY config*.yml ./

# Version stamp: the image has no .git (excluded from the build context), so
# `git rev-parse` cannot run in the container. Stamp the image BUILD DATE into
# a VERSION file at build time. To show a friendlier stamp, set the
# SCOUT_VERSION build arg or env var, which wins over this file. Kept as its
# own layer so it does not bust the pip cache above.
ARG SCOUT_VERSION=""
RUN if [ -n "$SCOUT_VERSION" ]; then printf '%s\n' "$SCOUT_VERSION" > /app/VERSION; \
    else date -u '+build %Y-%m-%d %H:%M UTC' > /app/VERSION; fi

# The bundled example seed shows the expected shape of the JSON store. It is a
# ONE-TIME seed for the database (migrate seeds a table only when it is empty);
# live state is database-only afterwards. Replace it with your own data at
# runtime — nothing personal ships in the image.
COPY seed_localdb.example/ seed_localdb.example/

CMD ["sh", "-c", "python -m scout.migrate --seed-dir seed_localdb.example && python -m scout.telegram_bot"]
