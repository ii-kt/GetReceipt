FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_EXECUTABLE=/usr/bin/google-chrome \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-noto-cjk \
        gnupg \
        wget \
        xvfb \
        xauth \
    && wget --quiet --output-document=- \
        https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --batch --dearmor --output /usr/share/keyrings/google-chrome.gpg \
    && gpg --batch --show-keys --with-colons /usr/share/keyrings/google-chrome.gpg \
        | grep -q "fpr:::::::::EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796:" \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    # google-chrome-stable depends on wget; purging wget would remove Chrome.
    && apt-get purge -y --auto-remove gnupg \
    && rm -f /etc/apt/sources.list.d/google-chrome.list \
        /usr/share/keyrings/google-chrome.gpg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY cloud/requirements.txt /app/cloud/requirements.txt
COPY worker/requirements.txt /app/worker/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/worker/requirements.txt

COPY cloud/src /app/cloud/src
COPY worker /app/worker
COPY scripts/verify_chrome_worker.py /app/scripts/verify_chrome_worker.py

RUN useradd --create-home --uid 10001 getreceipt \
    && mkdir -p /var/lib/getreceipt \
    && chown -R getreceipt:getreceipt /var/lib/getreceipt /app

USER getreceipt
VOLUME ["/var/lib/getreceipt"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=8s --start-period=45s --retries=3 \
    CMD ["python", "/app/worker/healthcheck.py"]

CMD ["python", "-m", "uvicorn", "worker.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
