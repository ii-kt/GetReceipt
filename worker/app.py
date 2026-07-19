from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
from pathlib import Path
from threading import Event
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import find_browser_executable  # noqa: E402
from src.automation.credentials import service_credentials  # noqa: E402
from src.automation.microsoft_graph import TokutenGraphFetcher  # noqa: E402
from src.automation.providers import build_receipt_fetcher  # noqa: E402
from src.jobs.inbox import ChallengeResponseInbox  # noqa: E402
from src.jobs.store import SQLiteJobStore  # noqa: E402
from src.oauth.microsoft import (  # noqa: E402
    MicrosoftOAuthConfig,
    MicrosoftOAuthManager,
    MicrosoftTokenStore,
)
from src.runtime_security import (  # noqa: E402
    WorkerInstanceLease,
    cleanup_stale_downloads,
    harden_private_tree,
    secure_sqlite_files,
    use_private_process_umask,
)
from src.storage.drive_storage import DriveStorage  # noqa: E402
from src.storage.sqlite_backup import SQLiteBackupManager  # noqa: E402
from src.workers.backup_lifecycle import SQLiteBackupLifecycle  # noqa: E402
from src.workers.http_api import WorkerASGIApp  # noqa: E402
from src.workers.runner import ReceiptWorker, WorkerRuntimeConfig  # noqa: E402
from src.workers.service import WorkerService  # noqa: E402


LOGGER = logging.getLogger(__name__)
_FATAL_RESTART_REQUESTED = Event()


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _bounded_int_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else int(default)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _request_supervisor_restart() -> None:
    """Ask Uvicorn to stop so Compose's restart policy can recover it."""

    if _FATAL_RESTART_REQUESTED.is_set():
        return
    _FATAL_RESTART_REQUESTED.set()
    LOGGER.critical(
        "Fatal worker health failure requires a supervised process restart"
    )
    os.kill(os.getpid(), signal.SIGTERM)


def _provider_credentials() -> dict[str, Any]:
    raw = _required_env("GETRECEIPT_PROVIDER_CREDENTIALS_JSON")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "GETRECEIPT_PROVIDER_CREDENTIALS_JSON must be valid JSON"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError("GETRECEIPT_PROVIDER_CREDENTIALS_JSON must be an object")
    return value


def _microsoft_oauth_manager(
    *,
    database_path: Path,
    owner_id: str,
) -> MicrosoftOAuthManager | None:
    names = (
        "GETRECEIPT_MICROSOFT_CLIENT_ID",
        "GETRECEIPT_MICROSOFT_CLIENT_SECRET",
        "GETRECEIPT_MICROSOFT_REDIRECT_URI",
        "GETRECEIPT_TOKEN_ENCRYPTION_KEY",
    )
    values = {name: str(os.getenv(name) or "").strip() for name in names}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Microsoft OAuth environment is incomplete: " + ", ".join(missing)
        )
    config = MicrosoftOAuthConfig(
        client_id=values["GETRECEIPT_MICROSOFT_CLIENT_ID"],
        client_secret=values["GETRECEIPT_MICROSOFT_CLIENT_SECRET"],
        redirect_uri=values["GETRECEIPT_MICROSOFT_REDIRECT_URI"],
        encryption_key=values["GETRECEIPT_TOKEN_ENCRYPTION_KEY"],
        tenant=str(os.getenv("GETRECEIPT_MICROSOFT_TENANT") or "consumers"),
    )
    token_store = MicrosoftTokenStore(
        database_path=database_path,
        owner_id=owner_id,
        encryption_key=config.encryption_key,
    )
    return MicrosoftOAuthManager(
        config=config,
        token_store=token_store,
    )


def build_app() -> WorkerASGIApp:
    api_token = _required_env("GETRECEIPT_API_TOKEN")
    owner_id = _required_env("GETRECEIPT_OWNER_ID")
    use_private_process_umask()
    database_path = Path(
        os.getenv("GETRECEIPT_DATABASE_PATH", "/var/lib/getreceipt/jobs.sqlite3")
    ).resolve()
    profile_root = Path(
        os.getenv("GETRECEIPT_PROFILE_ROOT", "/var/lib/getreceipt/profiles")
    ).resolve()
    download_root = Path(
        os.getenv("GETRECEIPT_DOWNLOAD_ROOT", "/var/lib/getreceipt/downloads")
    ).resolve()
    instance_lock_path = Path(
        os.getenv(
            "GETRECEIPT_INSTANCE_LOCK_PATH",
            str(database_path.parent / "worker.instance.lock"),
        )
    ).resolve()
    instance_lease = WorkerInstanceLease(instance_lock_path)
    instance_lease.acquire()
    atexit.register(instance_lease.release)
    store: SQLiteJobStore | None = None
    try:
        harden_private_tree(profile_root)
        cleanup_stale_downloads(download_root)
        secure_sqlite_files(database_path)

        chrome_executable = find_browser_executable()
        if not chrome_executable:
            raise RuntimeError("Google Chrome is required for the acquisition worker")
        os.environ["BROWSER_EXECUTABLE"] = chrome_executable

        credentials = _provider_credentials()
        store = SQLiteJobStore(database_path)
        inbox = ChallengeResponseInbox()
        microsoft_oauth = _microsoft_oauth_manager(
            database_path=database_path,
            owner_id=owner_id,
        )
        secure_sqlite_files(database_path)

        def fetcher_factory(
            service_id: str,
            browser: Any,
            service_secrets: dict[str, str],
        ) -> Any:
            if service_id == "tokuten" and microsoft_oauth is not None:
                return TokutenGraphFetcher(microsoft_oauth.access_token)
            return build_receipt_fetcher(
                service_id,
                browser,
                service_secrets,
            )

        worker = ReceiptWorker(
            store=store,
            inbox=inbox,
            config=WorkerRuntimeConfig(
                owner_id=owner_id,
                profile_root=profile_root,
                download_root=download_root,
            ),
            storage_factory=lambda: DriveStorage.from_secrets(None),
            credentials_factory=lambda service_id: service_credentials(
                credentials,
                service_id,
            ),
            fetcher_factory=fetcher_factory,
            fatal_callback=_request_supervisor_restart,
        )
        service = WorkerService(
            store=store,
            inbox=inbox,
            worker=worker,
            owner_id=owner_id,
            microsoft_oauth=microsoft_oauth,
        )
        backup_lifecycle = SQLiteBackupLifecycle(
            manager=SQLiteBackupManager(
                database_path=database_path,
                persistent_root=database_path.parent,
                retention_count=_bounded_int_env(
                    "GETRECEIPT_BACKUP_RETENTION_COUNT",
                    default=14,
                    minimum=2,
                    maximum=365,
                ),
            ),
            interval_seconds=_bounded_int_env(
                "GETRECEIPT_BACKUP_INTERVAL_SECONDS",
                default=6 * 60 * 60,
                minimum=5 * 60,
                maximum=24 * 60 * 60,
            ),
            retry_interval_seconds=_bounded_int_env(
                "GETRECEIPT_BACKUP_RETRY_SECONDS",
                default=60,
                minimum=10,
                maximum=60 * 60,
            ),
            fatal_failure_threshold=_bounded_int_env(
                "GETRECEIPT_BACKUP_FATAL_FAILURES",
                default=3,
                minimum=1,
                maximum=10,
            ),
            fatal_callback=_request_supervisor_restart,
        )
        return WorkerASGIApp(
            service=service,
            api_token=api_token,
            shutdown_callback=store.close,
            backup_lifecycle=backup_lifecycle,
        )
    except Exception:
        if store is not None:
            store.close()
        atexit.unregister(instance_lease.release)
        instance_lease.release()
        raise


app = build_app()
