from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
APP_MAIN = ROOT_DIR / "app" / "main.js"
BACKEND_LOG = ROOT_DIR / "cloud" / "data" / "node-backend.log"


class BackendError(RuntimeError):
    pass


@dataclass
class BackendClient:
    url: str | None = None
    process: subprocess.Popen | None = None

    def ensure_started(self) -> str:
        if self.url and self._is_healthy(self.url):
            return self.url

        existing = self._discover()
        if existing:
            self.url = existing
            return existing

        node = self._node_executable()
        if not APP_MAIN.exists():
            raise BackendError(f"Node backend entry point was not found: {APP_MAIN}")

        BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = BACKEND_LOG.open("ab")
        env = os.environ.copy()
        env.update({
            "GETRECEIPT_NO_OPEN": "1",
            "GETRECEIPT_NO_REUSE": "1",
            "GETRECEIPT_KEEP_ALIVE": "1",
            "GETRECEIPT_BROWSER_HEADLESS": env.get("GETRECEIPT_BROWSER_HEADLESS", "1"),
            "BROWSER_NO_SANDBOX": env.get("BROWSER_NO_SANDBOX", "1"),
        })
        self.process = subprocess.Popen(
            [node, str(APP_MAIN)],
            cwd=str(ROOT_DIR),
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            close_fds=os.name != "nt",
        )

        deadline = time.time() + 25
        while time.time() < deadline:
            discovered = self._discover()
            if discovered:
                self.url = discovered
                return discovered
            if self.process.poll() is not None:
                raise BackendError(f"Node backend exited early. Check {BACKEND_LOG}")
            time.sleep(0.5)

        raise BackendError(f"Node backend did not become ready. Check {BACKEND_LOG}")

    def state(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/state")

    def start_download(self, *, service: str, year: int, month: int, file_format: str = "pdf", force: bool = False) -> dict[str, Any]:
        return self._request_json("POST", "/api/download", {
            "service": service,
            "year": year,
            "month": month,
            "format": file_format,
            "force": force,
        })

    def screenshot(self, *, service: str) -> bytes:
        query = urllib.parse.urlencode({"service": service})
        return self._request_bytes("GET", f"/api/browser/screenshot?{query}")

    def click(self, *, service: str, x: int, y: int) -> dict[str, Any]:
        return self._request_json("POST", "/api/browser/click", {"service": service, "x": x, "y": y})

    def text(self, *, service: str, text: str) -> dict[str, Any]:
        return self._request_json("POST", "/api/browser/text", {"service": service, "text": text})

    def key(self, *, service: str, key: str = "Enter") -> dict[str, Any]:
        return self._request_json("POST", "/api/browser/key", {"service": service, "key": key})

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = self._request_bytes(method, path, body)
        return json.loads(raw.decode("utf-8"))

    def _request_bytes(self, method: str, path: str, body: dict[str, Any] | None = None) -> bytes:
        base_url = self.ensure_started()
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise BackendError(detail or str(error)) from error
        except urllib.error.URLError as error:
            raise BackendError(str(error)) from error

    @staticmethod
    def _node_executable() -> str:
        configured = os.environ.get("GETRECEIPT_NODE") or os.environ.get("NODE_BINARY")
        if configured and Path(configured).exists():
            return configured
        found = shutil.which("node")
        if found:
            return found
        raise BackendError("Node.js was not found. Install Node.js or set GETRECEIPT_NODE.")

    @staticmethod
    def _is_healthy(url: str) -> bool:
        try:
            with urllib.request.urlopen(f"{url}/api/state", timeout=1.5) as response:
                if response.status != 200:
                    return False
                state = json.loads(response.read().decode("utf-8"))
                return "task" in state and "history" in state
        except Exception:
            return False

    @classmethod
    def _discover(cls) -> str | None:
        for port in range(18765, 18966):
            url = f"http://127.0.0.1:{port}"
            if cls._is_healthy(url):
                return url
        return None


