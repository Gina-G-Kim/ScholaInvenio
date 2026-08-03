# syntax=docker/dockerfile:1
#
# Multi-architecture image (amd64/arm64). No OS-specific binaries, so it
# builds on macOS and runs unmodified on Linux. The GUI is a web UI served
# by the container, so the host OS doesn't matter.
#
# Chromium is only used for the Google Scholar path (undetected-chromedriver).
# If you're only using the default OpenAlex provider, build with
# BUILD_BROWSER=0 to skip it and get a much smaller image.

FROM python:3.12-slim-bookworm

ARG BUILD_BROWSER=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8000 \
    CHROME_BINARY=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/home/scholar/chromedriver

WORKDIR /srv

# undetected-chromedriver expects a system Chrome/driver; letting it download
# its own inside a container tends to fail, so the distro packages are
# installed instead and their paths passed through as env vars.
RUN if [ "$BUILD_BROWSER" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver fonts-liberation ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# Don't run the container as root. /data is the volume mount point.
RUN useradd --create-home --uid 10001 scholar \
 && mkdir -p /data \
 && chown -R scholar:scholar /srv /data
# undetected-chromedriver patches the driver binary **in place** to strip
# automation fingerprints. /usr/bin is root-owned, so a non-root user can't
# write there — hence a writable copy in the user's home, pointed to above.
RUN if [ -x /usr/bin/chromedriver ]; then \
      cp /usr/bin/chromedriver /home/scholar/chromedriver \
      && chown scholar:scholar /home/scholar/chromedriver \
      && chmod 0755 /home/scholar/chromedriver; \
    fi
USER scholar
ENV HOME=/home/scholar

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=3).status==200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
