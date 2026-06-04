FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/claudeailab/dockwatch"
LABEL org.opencontainers.image.description="Docker container update manager"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8090

CMD ["gunicorn", "--bind", "0.0.0.0:8090", "--workers", "1", "--threads", "4", "--timeout", "120", "main:app"]
