from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.ui.module_contract import UIModuleContractError, ensure_ui_module  # noqa: E402


def ui_module(*, version: int, include_renderer: bool) -> ModuleType:
    module = ModuleType("test_ui_styles")
    module.UI_API_VERSION = version
    if include_renderer:
        module.render_archive_hero = lambda: None
    return module


class UIModuleContractTest(unittest.TestCase):
    def test_current_module_does_not_reload(self) -> None:
        current = ui_module(version=2, include_renderer=True)

        result = ensure_ui_module(
            current,
            expected_version=2,
            required_callables=("render_archive_hero",),
            reload_module=lambda _module: self.fail("current module must not reload"),
        )

        self.assertIs(result, current)

    def test_stale_module_reloads_and_revalidates(self) -> None:
        stale = ui_module(version=1, include_renderer=False)
        current = ui_module(version=2, include_renderer=True)
        reloads: list[ModuleType] = []

        def reload_module(module: ModuleType) -> ModuleType:
            reloads.append(module)
            return current

        result = ensure_ui_module(
            stale,
            expected_version=2,
            required_callables=("render_archive_hero",),
            reload_module=reload_module,
        )

        self.assertIs(result, current)
        self.assertEqual(reloads, [stale])

    def test_invalid_reloaded_module_fails_with_contract_details(self) -> None:
        stale = ui_module(version=1, include_renderer=False)

        with self.assertRaisesRegex(
            UIModuleContractError,
            "UI_API_VERSION.*render_archive_hero",
        ):
            ensure_ui_module(
                stale,
                expected_version=2,
                required_callables=("render_archive_hero",),
                reload_module=lambda module: module,
            )


if __name__ == "__main__":
    unittest.main()
