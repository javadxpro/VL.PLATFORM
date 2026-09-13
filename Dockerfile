FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('PORT','5000') + '/api/app_info')" || exit 1

CMD ["sh", "-c", "gunicorn -k gevent -w 1 -b 0.0.0.0:$PORT server:app"]
