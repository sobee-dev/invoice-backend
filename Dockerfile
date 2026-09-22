# ── Dockerfile ──────────────────────────────────────────────────────────────
# This is a RECIPE. It does not run anything by itself — it describes,
# step by step, how to build a "frozen meal" (called an image) that
# contains your app + everything it needs to run.

# 1. Start from a base that already has Python installed.
#    "slim" = a smaller, lighter version (less junk, faster to download).
FROM python:3.12-slim

# 2. Set some basic Python behavior for running inside a container.
#    PYTHONDONTWRITEBYTECODE: don't create .pyc cache files (not needed here)
#    PYTHONUNBUFFERED: print logs immediately instead of holding them back
#    (this matters so `docker logs` shows things in real time)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 3. Create a folder inside the container called /app, and make it the
#    "current folder" for every command that follows.
WORKDIR /app

# 4. Install OS-level tools some Python packages need to build
#    (psycopg2 for Postgres, for example, needs a C compiler).
#    We clean up the package cache right after to keep the image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 5. Copy ONLY requirements.txt first (not the whole app yet).
#    This is a deliberate trick: Docker caches each step. If your code
#    changes but requirements.txt doesn't, Docker skips reinstalling
#    everything and reuses the cached install — much faster rebuilds.
COPY requirements.txt .

# 6. Install your Python packages (Django, DRF, gunicorn, psycopg2, etc).
RUN pip install --no-cache-dir -r requirements.txt

# 7. NOW copy the rest of your actual application code into the image.
COPY . .

# 8. Tell Docker (and humans reading this file) which port the app
#    listens on inside the container. This is documentation — it
#    doesn't actually open the port by itself (docker-compose does that).
EXPOSE 8000

# 9. Copy in the entrypoint script and make it executable. This runs
#    `migrate` automatically, then starts gunicorn — see entrypoint.sh
#    for exactly what it does and why.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]