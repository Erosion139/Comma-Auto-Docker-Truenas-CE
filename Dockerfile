FROM python:3.12-slim

# ffmpeg does the encoding. SSH keys are generated in Python (no ssh-keygen needed).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/comma-sync
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data \
    OUTPUT_DIR=/output \
    WEBUI_PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/data", "/output"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBUI_PORT\",\"8080\")}/api/health', timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
