FROM python:3.14-slim

# Fixed uid/gid so the NAS bind-mount chown is predictable: `chown -R 1000:1000`
# on the host data directory is all that is required.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin app

WORKDIR /app

# Dependencies before source, so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY live_server.py poll_unifi.py ./
COPY unifi_lib/ ./unifi_lib/
COPY static/ ./static/

# The database lives on a mounted volume, never in the image.
RUN mkdir -p /data && chown 1000:1000 /data
VOLUME ["/data"]

ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8787 \
    UNIFI_DB_PATH=/data/unifi_clients.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8787

# `/` serves the static HTML with no controller round-trip, so this stays cheap.
# python:3.14-slim ships neither curl nor wget; urllib avoids adding a package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('BIND_PORT','8787') + '/', timeout=4).status == 200 else 1)"]

CMD ["python", "live_server.py"]
