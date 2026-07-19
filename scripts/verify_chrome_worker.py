from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import ManagedBrowser, find_browser_executable  # noqa: E402


class SmokePage(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = (
            b"<!doctype html><html><head><title>GetReceipt Chrome smoke</title></head>"
            b"<body><main id='ready'>ready</main>"
            b"<form id='viewer-form'><input id='viewer-input' autocomplete='off'>"
            b"<button type='submit'>send</button></form><div id='viewer-result'></div>"
            b"<script>document.getElementById('viewer-form').addEventListener("
            b"'submit',event=>{event.preventDefault();document.getElementById("
            b"'viewer-result').textContent=document.getElementById("
            b"'viewer-input').value;});</script></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def verify() -> dict[str, object]:
    executable = find_browser_executable()
    if not executable:
        import os

        probes = {
            candidate: Path(candidate).exists()
            for candidate in (
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/opt/google/chrome/google-chrome",
                "/opt/google/chrome/chrome",
            )
        }
        raise RuntimeError(
            "Google Chrome was not found: "
            f"BROWSER_EXECUTABLE={os.getenv('BROWSER_EXECUTABLE')!r}, "
            f"CHROME_BIN={os.getenv('CHROME_BIN')!r}, probes={probes}"
        )
    executable_name = Path(executable).name.lower()
    if executable_name not in {"chrome.exe", "google-chrome", "google-chrome-stable"}:
        raise RuntimeError(f"selected browser is not Google Chrome: {executable_name}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), SmokePage)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    temporary = tempfile.TemporaryDirectory(
        prefix="getreceipt-chrome-smoke-",
        ignore_cleanup_errors=True,
    )
    first: ManagedBrowser | None = None
    second: ManagedBrowser | None = None
    try:
        root = Path(temporary.name)
        profile = root / "profile"
        first = ManagedBrowser(
            profile_dir=profile,
            download_dir=root / "downloads-1",
        )
        first.navigate(url)
        product = str(first.connection.send("Browser.getVersion").get("product") or "")
        if "Chrome/" not in product:
            raise RuntimeError(f"browser product is not Chrome: {product}")
        input_point = first.evaluate(
            """(() => {
              const rect = document.getElementById("viewer-input").getBoundingClientRect();
              return {
                x: Math.round(rect.left + rect.width / 2),
                y: Math.round(rect.top + rect.height / 2)
              };
            })()"""
        )
        first.click_current_page(
            int(input_point["x"]),
            int(input_point["y"]),
        )
        first.insert_text_current_page("iphone-viewer-roundtrip")
        input_state = first.evaluate(
            """({
              value: document.getElementById("viewer-input").value,
              active: document.activeElement && document.activeElement.id
            })"""
        )
        first.press_key_current_page("Enter")
        time.sleep(0.15)
        viewer_result = first.evaluate(
            'document.getElementById("viewer-result").textContent'
        )
        if viewer_result != "iphone-viewer-roundtrip":
            raise RuntimeError(
                "Chrome viewer input did not reach the live page: "
                f"input_state={input_state!r}, result={viewer_result!r}"
            )
        viewer_frame = first.screenshot_current_page()
        if not viewer_frame.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Chrome viewer frame is not a PNG")
        first.evaluate(
            """(() => {
              localStorage.setItem("getreceipt-profile-smoke", "persisted");
              return localStorage.getItem("getreceipt-profile-smoke");
            })()"""
        )
        first.close(clear_profile=False)
        first = None

        second = ManagedBrowser(
            profile_dir=profile,
            download_dir=root / "downloads-2",
        )
        second.navigate(url)
        persisted = second.evaluate(
            'localStorage.getItem("getreceipt-profile-smoke")'
        )
        if persisted != "persisted":
            raise RuntimeError("Chrome profile did not persist across browser restart")
        second.close(clear_profile=False)
        second = None
        return {
            "ok": True,
            "browser_executable": executable,
            "browser_product": product,
            "profile_persisted": True,
            "viewer_input_roundtrip": True,
            "viewer_frame_png": True,
        }
    finally:
        if first is not None:
            first.close(clear_profile=False)
        if second is not None:
            second.close(clear_profile=False)
        temporary.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Google Chrome selection and persistent worker profiles."
    )
    parser.parse_args()
    print(json.dumps(verify(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
