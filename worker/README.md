# GetReceipt persistent worker

This service moves provider browser automation out of the Streamlit request
process. Run exactly one worker process for this personal account. The supported
acquisition browser is **Google Chrome**; Chromium is not used by the worker.
The owner operates the product from Google Chrome on iPhone; this service keeps
the independent desktop Chrome session alive when the mobile tab is suspended.

## Required environment

```text
GETRECEIPT_API_TOKEN                 at least 32 random characters
GETRECEIPT_OWNER_ID                  stable opaque owner identifier
GETRECEIPT_PROVIDER_CREDENTIALS_JSON JSON object with epos/commufa/tokuten/webbilling sections
GOOGLE_SERVICE_ACCOUNT_JSON          Google service-account JSON
BROWSER_EXECUTABLE                   Google Chrome path (production Compose fixes /usr/bin/google-chrome)
```

Microsoft Graph mode also requires:

```text
GETRECEIPT_MICROSOFT_CLIENT_ID
GETRECEIPT_MICROSOFT_CLIENT_SECRET
GETRECEIPT_MICROSOFT_REDIRECT_URI
GETRECEIPT_MICROSOFT_TENANT
GETRECEIPT_TOKEN_ENCRYPTION_KEY
```

The redirect URI is the exact Streamlit app root, including its trailing slash.
Only delegated `Mail.Read` is requested. Refresh tokens are encrypted before
being stored in the worker database.

When Compose `env_file` is used, wrap JSON, provider passwords, client secrets,
and encryption keys in outer single quotes as shown in
`deploy/worker.env.example`. Compose removes those outer quotes and otherwise
keeps `$`, `#`, and backslashes literal.

Persistent paths default to:

```text
/var/lib/getreceipt/jobs.sqlite3
/var/lib/getreceipt/profiles
/var/lib/getreceipt/downloads
```

Override them with `GETRECEIPT_DATABASE_PATH`,
`GETRECEIPT_PROFILE_ROOT`, and `GETRECEIPT_DOWNLOAD_ROOT`.
`GETRECEIPT_INSTANCE_LOCK_PATH` defaults beside the database and prevents a
second worker process from controlling the same profiles.

The worker also creates verified SQLite online snapshots under the private
`.sqlite-backups` directory beside the database. The defaults retain 14
snapshots, create one every six hours, retry a failed periodic backup after 60
seconds, and treat three consecutive failures as fatal. They can be bounded
with:

```text
GETRECEIPT_BACKUP_RETENTION_COUNT
GETRECEIPT_BACKUP_INTERVAL_SECONDS
GETRECEIPT_BACKUP_RETRY_SECONDS
GETRECEIPT_BACKUP_FATAL_FAILURES
```

One snapshot must succeed before acquisition starts. Normal shutdown stops the
acquisition thread, publishes a final snapshot, and then closes SQLite.
Periodic backup failures make `/healthz` return 503 without exposing paths or
exception messages. Repeated backup failure or an unexpected worker-loop exit
requests a graceful process shutdown; Compose's `restart: unless-stopped`
then starts a fresh container. This avoids mounting the Docker socket or adding
a privileged auto-heal sidecar. Same-volume snapshots do not replace encrypted
disk snapshots or off-host backups.

## Container

From the repository root:

```bash
docker build -f worker/Dockerfile -t getreceipt-worker .
docker run --rm -p 127.0.0.1:8080:8080 \
  -v getreceipt-data:/var/lib/getreceipt \
  --env-file worker.env \
  getreceipt-worker
```

Terminate TLS at a managed load balancer or reverse proxy. The public worker
endpoint must be HTTPS. Do not publish port 8080, Chrome CDP ports, or a VNC
port directly.

Configure the Streamlit app with the same token and owner ID:

```toml
[receipt_worker]
base_url = "https://your-worker.example"
api_token = "same high-entropy token"
owner_id = "same opaque owner identifier"
```

The Streamlit app itself must be private or protected by OIDC before enabling
the worker.

After installing Chrome on a host, verify the exact browser family and profile
persistence before adding provider credentials:

```bash
BROWSER_EXECUTABLE=/usr/bin/google-chrome \
  python scripts/verify_chrome_worker.py
```

Production uses the digest-pinned image and Caddy/Compose configuration in
[`deploy/`](../deploy/README.md). Do not expose port 8080, CDP, X11, or VNC.
Interactive provider screens are relayed as short-lived PNG frames and
click/key events through the authenticated Streamlit server; the bearer token
never enters the iPhone browser.

The authenticated `POST /v1/manual-receipts` route accepts a raw
`application/pdf` body with non-secret `service_id`, `target_month`, and
`confirmed` query metadata. The worker caps the body at 20 MiB, rejects
content encoding, revalidates the PDF, and uses the normal duplicate-check,
naming, Drive upload, and post-upload verification pipeline. PDF bytes and
worker credentials are not included in responses or application logs.
