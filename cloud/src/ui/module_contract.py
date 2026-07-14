from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib import invalidate_caches, reload
from threading import RLock
from types import ModuleType


_RELOAD_LOCK = RLock()


class UIModuleContractError(RuntimeError):
    """Raised when the loaded UI module cannot satisfy the app contract."""


def ensure_ui_module(
    module: ModuleType,
    *,
    expected_version: int,
    required_callables: Iterable[str],
    reload_module: Callable[[ModuleType], ModuleType] = reload,
) -> ModuleType:
    """Reload a stale Streamlit UI module only when its contract is outdated.

    Streamlit can rerun the entrypoint inside a long-lived Python process while
    imported modules remain in ``sys.modules``. A Cloud deploy may therefore
    execute a new entrypoint against an older stateless UI renderer. Checking a
    version and its callable surface keeps a hot deploy atomic from the app's
    point of view without reloading the module on every widget interaction.
    """

    required = tuple(dict.fromkeys(str(name) for name in required_callables))
    if not _contract_issues(module, expected_version, required):
        return module

    with _RELOAD_LOCK:
        if not _contract_issues(module, expected_version, required):
            return module
        invalidate_caches()
        refreshed = reload_module(module)
        issues = _contract_issues(refreshed, expected_version, required)
        if issues:
            raise UIModuleContractError(
                "UIモジュールの更新後もアプリ契約を満たしません: " + "、".join(issues)
            )
        return refreshed


def _contract_issues(
    module: ModuleType,
    expected_version: int,
    required_callables: tuple[str, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    current_version = getattr(module, "UI_API_VERSION", None)
    if current_version != expected_version:
        issues.append(f"UI_API_VERSION={current_version!r} (expected {expected_version})")
    missing = [name for name in required_callables if not callable(getattr(module, name, None))]
    if missing:
        issues.append("missing=" + ",".join(missing))
    return tuple(issues)
