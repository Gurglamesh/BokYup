# BokYup self-hosted server (headless FastAPI backend). Build context = repo root.
#   docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.12-slim

WORKDIR /srv
# argon2-cffi / cryptography / Pillow ship manylinux wheels, so no build toolchain needed.
COPY requirements.txt .
RUN grep -v -e '^pywebview' -e '^pytest' requirements.txt > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt pillow

COPY backend/ ./backend/

ENV BOKYUP_DATA_DIR=/data \
    BOKYUP_HOST=0.0.0.0 \
    BOKYUP_PORT=8756 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8756
# BOKYUP_API_TOKEN must be supplied at runtime — the server refuses to start without it.
CMD ["python", "-m", "backend.server"]
