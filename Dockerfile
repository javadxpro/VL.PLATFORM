FROM python:3.12-slim

# One layer for dependencies so app-code edits do not reinstall anything.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000 \
    VOLEXTURN_UPLOAD_FOLDER=/data/uploads \
    VOLEXTURN_DB_PATH=/data/database.db

WORKDIR /app
COPY . /app

# The image runs unprivileged: /data (uploads + sqlite) is the only thing it
# writes, so that is the one path that has to be owned by the service user.
# A named volume mounted at /data inherits these ownership/permissions, which is
# why the chown happens before the volume is declared.
RUN useradd --create-home --uid 10001 volex \
    && mkdir -p /data/uploads \
    && chown -R volex:volex /data /app \
    && python -m compileall -q backend server.py

USER volex
VOLUME ["/data"]
EXPOSE 5000

# /healthz answers without a database write, so the probe still tells the
# orchestrator something useful while the disk is busy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','5000'), timeout=4)"]

# Migrations run inside create_app() on boot; `--force` is not used so a restart
# never replays a one-off step.
CMD ["sh", "-c", "exec gunicorn -k gevent -w 1 --bind 0.0.0.0:$PORT --access-logfile - --error-logfile - server:app"]
