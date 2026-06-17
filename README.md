# GetReceipt

Streamlit cloud app for collecting receipt and statement files from an iPhone without starting the local PC.

## Runtime

- UI: `cloud/streamlit_app.py`
- Cloud container: `cloud/Dockerfile`
- Render config: `cloud/render.yaml`
- Google Drive destination: https://drive.google.com/drive/folders/1jwaMMK-KGIyUampBWOjRIY3BULuj6W-M

The Streamlit UI starts the Node worker in `app/main.js`. The worker uses `app/lib` and `app/config` for the existing acquisition automations.

## Required Secrets

Set the Google service account JSON in the cloud host as:

```text
GOOGLE_SERVICE_ACCOUNT_JSON
```

Also share the Google Drive receipt folder with the service account email as an editor.

## Deploy

Use Docker from the repository root:

```powershell
docker build -f cloud/Dockerfile -t getreceipt-cloud .
```

For Render, use `cloud/render.yaml`.
