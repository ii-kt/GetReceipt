from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import DATA_DIR


class BrowserAutomationError(RuntimeError):
    pass


def _path_exists(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    return str(path) if path.exists() else None


def find_browser_executable() -> str | None:
    for candidate in (
        os.getenv("BROWSER_EXECUTABLE"),
        os.getenv("CHROME_BIN"),
        os.getenv("CHROMIUM_BIN"),
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
    ):
        found = _path_exists(candidate)
        if found:
            return found

    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found

    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        found = _path_exists(candidate)
        if found:
            return found
    return None


def find_free_port(start_at: int = 19021) -> int:
    for port in range(start_at, start_at + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise BrowserAutomationError("取得用ブラウザの空きポートを確保できませんでした。")


class CDPConnection:
    def __init__(self, websocket_url: str):
        try:
            from websockets.sync.client import connect
        except Exception as error:
            raise BrowserAutomationError(
                "ブラウザ操作ライブラリが不足しています。requirements.txtを再インストールしてください。"
            ) from error

        self.websocket = connect(websocket_url, open_timeout=20, ping_interval=None)
        self.next_id = 1

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.websocket.send(json.dumps(payload))

        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.websocket.recv(timeout=max(0.1, deadline - time.time()))
            message = json.loads(raw)
            if message.get("id") != message_id:
                continue
            if "error" in message:
                detail = message["error"].get("message") or str(message["error"])
                raise BrowserAutomationError(f"{method} failed: {detail}")
            return message.get("result") or {}
        raise BrowserAutomationError(f"{method} timed out.")

    def close(self) -> None:
        try:
            self.websocket.close()
        except Exception:
            pass


class ManagedBrowser:
    def __init__(
        self,
        *,
        profile_dir: Path | None = None,
        download_dir: Path | None = None,
    ) -> None:
        self.profile_dir = profile_dir or DATA_DIR / "browser-profile"
        self.download_dir = download_dir or DATA_DIR / "browser-downloads"
        self.port: int | None = None
        self.process: subprocess.Popen[Any] | None = None
        self.connection: CDPConnection | None = None
        self.target_id: str | None = None
        self.session_id: str | None = None
        self.stderr_path = self.download_dir / "chromium-stderr.log"

    def ensure_started(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self.process = None
            self.connection = None
            self.target_id = None
            self.session_id = None

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        executable = find_browser_executable()
        if not executable:
            raise BrowserAutomationError(
                "取得用Chromiumを見つけられませんでした。packages.txtのchromium設定を確認してください。"
            )

        if self.port is None:
            self.port = find_free_port()

        if self.process is None:
            self._start_browser_with_fallbacks(executable)

        if self.connection is None:
            version = self._wait_for_version()
            self.connection = CDPConnection(version["webSocketDebuggerUrl"])
            try:
                self.connection.send(
                    "Browser.setDownloadBehavior",
                    {
                        "behavior": "allow",
                        "downloadPath": str(self.download_dir),
                        "eventsEnabled": False,
                    },
                )
            except BrowserAutomationError:
                pass
            self.connection.send("Target.setDiscoverTargets", {"discover": True})

    def _start_browser_with_fallbacks(self, executable: str) -> None:
        errors: list[str] = []
        for headless_arg in ("--headless=new", "--headless"):
            self._cleanup_profile_locks()
            self._launch_browser(executable, headless_arg)
            try:
                self._wait_for_version()
                return
            except BrowserAutomationError as error:
                errors.append(str(error))
                self._stop_process()
                self.port = find_free_port()
        raise BrowserAutomationError("取得用ブラウザを起動できませんでした: " + " / ".join(errors[-2:]))

    def _launch_browser(self, executable: str, headless_arg: str) -> None:
        assert self.port is not None
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path.write_text("", encoding="utf-8")
        args = [
            executable,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-features=Crashpad",
            "--disable-gpu",
            "--disable-popup-blocking",
            "--disable-setuid-sandbox",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-sandbox",
            "--no-zygote",
            "--window-size=1280,900",
            headless_arg,
            "about:blank",
        ]
        stderr_handle = self.stderr_path.open("ab")
        try:
            self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_handle)
        finally:
            stderr_handle.close()

    def _cleanup_profile_locks(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "LOCK"):
            try:
                (self.profile_dir / name).unlink(missing_ok=True)
            except Exception:
                pass

    def _stop_process(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None

    def _stderr_tail(self) -> str:
        try:
            text = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        text = text.strip()
        if not text:
            return ""
        return text[-1200:]

    def _wait_for_version(self) -> dict[str, Any]:
        assert self.port is not None
        url = f"http://127.0.0.1:{self.port}/json/version"
        deadline = time.time() + 30
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                detail = self._stderr_tail()
                suffix = f" / Chromium stderr: {detail}" if detail else ""
                raise BrowserAutomationError(f"取得用ブラウザが起動直後に終了しました(exit {self.process.returncode}){suffix}")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as error:
                last_error = error
                time.sleep(0.25)
        detail = self._stderr_tail()
        suffix = f" / Chromium stderr: {detail}" if detail else ""
        raise BrowserAutomationError(f"取得用ブラウザに接続できませんでした: {last_error}{suffix}")

    def ensure_page(self) -> str:
        self.ensure_started()
        assert self.connection is not None
        if self.session_id:
            return self.session_id

        targets = self.connection.send("Target.getTargets").get("targetInfos", [])
        pages = [
            target for target in targets
            if target.get("type") == "page" and not str(target.get("url", "")).startswith(("devtools://", "chrome://"))
        ]
        target_id = pages[0]["targetId"] if pages else self.connection.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        attached = self.connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.target_id = target_id
        self.session_id = attached["sessionId"]
        for method in ("Runtime.enable", "Page.enable", "Network.enable"):
            self.connection.send(method, session_id=self.session_id)
        return self.session_id

    def attach_to_target(self, target_id: str) -> str:
        self.ensure_started()
        assert self.connection is not None
        if self.target_id == target_id and self.session_id:
            return self.session_id
        attached = self.connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.target_id = target_id
        self.session_id = attached["sessionId"]
        for method in ("Runtime.enable", "Page.enable", "Network.enable"):
            self.connection.send(method, session_id=self.session_id)
        return self.session_id

    def get_targets(self) -> list[dict[str, Any]]:
        self.ensure_started()
        assert self.connection is not None
        return self.connection.send("Target.getTargets").get("targetInfos", [])

    def switch_to_page(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        pages = [
            target for target in self.get_targets()
            if target.get("type") == "page"
            and not str(target.get("url", "")).startswith(("devtools://", "chrome://", "edge://"))
        ]
        matches = [target for target in pages if predicate(target)]
        if not matches:
            return None
        target = matches[-1]
        self.attach_to_target(target["targetId"])
        assert self.connection is not None
        self.connection.send("Target.activateTarget", {"targetId": target["targetId"]})
        return target

    def navigate(self, url: str, wait_seconds: float = 1.0) -> None:
        session_id = self.ensure_page()
        assert self.connection is not None
        self.connection.send("Page.navigate", {"url": url}, session_id=session_id)
        time.sleep(wait_seconds)

    def evaluate(self, expression: str, *, timeout: float = 30) -> Any:
        session_id = self.ensure_page()
        assert self.connection is not None
        result = self.connection.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
                "timeout": int(timeout * 1000),
            },
            session_id=session_id,
            timeout=timeout + 5,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"].get("text", "JavaScript evaluation failed.")
            raise BrowserAutomationError(detail)
        remote = result.get("result", {})
        return remote.get("value")

    def click_at(self, x: int, y: int) -> None:
        session_id = self.ensure_page()
        assert self.connection is not None
        for event_type, button in (("mouseMoved", "none"), ("mousePressed", "left"), ("mouseReleased", "left")):
            self.connection.send(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": x, "y": y, "button": button, "clickCount": 1},
                session_id=session_id,
            )

    def insert_text(self, text: str) -> None:
        session_id = self.ensure_page()
        assert self.connection is not None
        self.connection.send("Input.insertText", {"text": text}, session_id=session_id)

    def press_key(self, key: str = "Enter") -> None:
        session_id = self.ensure_page()
        assert self.connection is not None
        key_map = {"Enter": ("Enter", 13), "Escape": ("Escape", 27), "Tab": ("Tab", 9)}
        code, virtual_key = key_map.get(key, (key, 0))
        params = {"key": key, "code": code, "windowsVirtualKeyCode": virtual_key, "nativeVirtualKeyCode": virtual_key}
        self.connection.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **params}, session_id=session_id)
        self.connection.send("Input.dispatchKeyEvent", {"type": "keyUp", **params}, session_id=session_id)

    def screenshot(self) -> bytes:
        session_id = self.ensure_page()
        assert self.connection is not None
        result = self.connection.send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            session_id=session_id,
            timeout=20,
        )
        return base64.b64decode(result.get("data", ""))

    def print_to_pdf(self, file_path: Path, **options: Any) -> Path:
        session_id = self.ensure_page()
        assert self.connection is not None
        result = self.connection.send(
            "Page.printToPDF",
            {
                "printBackground": True,
                "preferCSSPageSize": True,
                "marginTop": 0.4,
                "marginBottom": 0.4,
                "marginLeft": 0.35,
                "marginRight": 0.35,
                **options,
            },
            session_id=session_id,
            timeout=60,
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(base64.b64decode(result.get("data", "")))
        return file_path

    def page_summary(self) -> dict[str, Any]:
        return self.evaluate(
            """(() => {
              const text = document.body ? document.body.innerText.replace(/\\s+/g, " ").slice(0, 10000) : "";
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
              };
              const visibleInputs = [...document.querySelectorAll("input, select, button, a, textarea, [role='searchbox']")]
                .filter(visible)
                .slice(0, 100)
                .map((el) => ({
                  tag: el.tagName.toLowerCase(),
                  type: el.getAttribute("type") || "",
                  text: (el.innerText || el.value || el.placeholder || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 100),
                  href: el.href || ""
                }));
              return {
                url: location.href,
                title: document.title,
                text,
                passwordFields: document.querySelectorAll("input[type='password']").length,
                visibleInputs
              };
            })()"""
        ) or {}

    def cookies_for(self, url: str) -> list[dict[str, Any]]:
        session_id = self.ensure_page()
        assert self.connection is not None
        return self.connection.send("Network.getCookies", {"urls": [url]}, session_id=session_id).get("cookies", [])

    def clear_downloads(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        for child in self.download_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def wait_for_download(self, extension: str, marker_time: float, timeout_seconds: float = 90) -> Path | None:
        suffix = "." + extension.lower().lstrip(".")
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            candidates = [
                child for child in self.download_dir.iterdir()
                if child.is_file()
                and not child.name.endswith((".crdownload", ".tmp"))
                and child.stat().st_mtime >= marker_time - 1
            ]
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            preferred = next((path for path in candidates if path.name.lower().endswith(suffix)), None)
            selected = preferred or (candidates[0] if candidates else None)
            if selected:
                return self._wait_for_stable_file(selected)
            time.sleep(0.3)
        return None

    def _wait_for_stable_file(self, file_path: Path, stable_seconds: float = 0.5) -> Path:
        last_size = -1
        stable_since = time.time()
        while time.time() - stable_since < stable_seconds:
            size = file_path.stat().st_size
            if size != last_size:
                last_size = size
                stable_since = time.time()
            time.sleep(0.15)
        return file_path

    def close(self, *, clear_profile: bool = False) -> None:
        if self.connection:
            try:
                self.connection.send("Browser.close", timeout=5)
            except Exception:
                self.connection.close()
        self.connection = None
        self.process = None
        self.target_id = None
        self.session_id = None
        if clear_profile and self.profile_dir.exists():
            shutil.rmtree(self.profile_dir, ignore_errors=True)
