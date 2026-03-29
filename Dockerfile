FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=5189 \
    APP_HOST=0.0.0.0 \
    TS_SOCKET=/var/run/tailscale/tailscaled.sock \
    TAILSCALE_SOCKET=/var/run/tailscale/tailscaled.sock \
    TS_STATE_DIR=/var/lib/tailscale \
    TS_MONITOR_DB_PATH=/data/tailscale_monitor.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg iproute2 iptables kmod \
    && install -m 0755 -d /usr/share/keyrings \
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
        -o /etc/apt/sources.list.d/tailscale.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends tailscale \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py README.md ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data /var/lib/tailscale /var/run/tailscale

EXPOSE 5189

VOLUME ["/data", "/var/lib/tailscale"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:5189/health', timeout=3); sys.exit(0)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
