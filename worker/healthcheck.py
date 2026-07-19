from __future__ import annotations

import json
import os
import urllib.request


def main() -> int:
    token = str(os.getenv("GETRECEIPT_API_TOKEN") or "")
    owner = str(os.getenv("GETRECEIPT_OWNER_ID") or "")
    if len(token) < 32 or not owner:
        return 1
    request = urllib.request.Request(
        "http://127.0.0.1:8080/healthz",
        headers={
            "Authorization": f"Bearer {token}",
            "X-GetReceipt-Owner": owner,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return 1
    return 0 if response.status == 200 and payload.get("worker_running") else 1


if __name__ == "__main__":
    raise SystemExit(main())

