#!/usr/bin/env bash
#
# Run the canonical server in the foreground.
#
# This is what the README tells you to use for development; production uses the
# Dockerfile / Procfile / render.yaml, which run gunicorn against the same
# `server:app`. If this script stops working, installing this app stops working,
# so it stays deliberately boring: no virtualenv creation, no dependency install,
# no opinion about where your database lives.
#
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] && [ -x .venv/bin/python ] && export PATH="$PWD/.venv/bin:$PATH"
command -v python3 >/dev/null || { echo "python3 not found (3.11+ required)"; exit 1; }

# The app refuses to *boot* in production without a secret key, and warns (loudly)
# without one anywhere else. A throwaway key is right here and never right in prod,
# so this is a development default, not a fallback you can ship.
export VOLEXTURN_SECRET_KEY="${VOLEXTURN_SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')}"
export VOLEXTURN_ENV="${VOLEXTURN_ENV:-development}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-5000}"

# Fail loudly on an install that is missing something, before binding a port.
# `migrate` runs inside create_app() on every boot, so this is a preflight report,
# not a migration step; it exits 1 on a broken schema or a missing optional feature.
python3 -m backend doctor

# Debug mode is Flask's server, for developers only.
#   python3 -m backend run            # threaded, reload off
#   VOLEXTURN_DEBUG=1 …               # debugger + reloader (development only)
#   PORT=8080 …                       # listen elsewhere
# Socket.IO works here via threading; gevent/eventlet are used when installed.
exec python3 -m backend run
