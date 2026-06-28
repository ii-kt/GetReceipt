from __future__ import annotations

import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ReadmeCloudOperationTest(unittest.TestCase):
    def test_readme_explains_streamlit_cloud_sleep_and_url_contract(self) -> None:
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("公開アプリURLは固定です", readme)
        self.assertIn("12時間アクセスがないアプリはスリープ", readme)
        self.assertIn("HTTPステータス `200` だけを合格条件にしません", readme)
        self.assertIn("GetReceipt", readme)
        self.assertIn("自動取得", readme)


if __name__ == "__main__":
    unittest.main()
