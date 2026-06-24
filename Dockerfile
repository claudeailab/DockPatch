FROM python:3.12.10-slim

LABEL org.opencontainers.image.source="https://github.com/claudeailab/dockpatch"
LABEL org.opencontainers.image.description="Docker container update manager"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8093

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8093/')" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8093", "--workers", "1", "--threads", "4", "--timeout", "120", "--worker-class", "gthread", "main:app"]
