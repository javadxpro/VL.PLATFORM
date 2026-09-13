# One worker on purpose: Socket.IO rooms and the presence map are in-process, so
# a second worker would split them (users would stop seeing each other).
# Scale by running more instances behind a message queue, not more workers.
# --timeout is raised so a large upload on a slow connection survives.
web: gunicorn -k gevent -w 1 --timeout 120 --graceful-timeout 30 -b 0.0.0.0:$PORT server:app
