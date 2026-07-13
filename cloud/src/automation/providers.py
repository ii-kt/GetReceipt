from __future__ import annotations

from typing import Any, Protocol

from .browser_session import ManagedBrowser


class ReceiptFetcher(Protocol):
    def fetch_pdf(self, target_month: str) -> Any: ...


def build_receipt_fetcher(
    service_id: str,
    browser: ManagedBrowser,
    credentials: dict[str, str],
) -> ReceiptFetcher:
    """Create the provider driver without coupling it to Streamlit or Drive."""

    if service_id == "epos":
        from .epos import EposAutoFetcher

        return EposAutoFetcher(browser, credentials=credentials)

    if service_id == "commufa":
        from .official_sites import CommufaAutoFetcher

        return CommufaAutoFetcher(browser, credentials=credentials)

    if service_id == "tokuten":
        from .official_sites import TokutenAutoFetcher

        return TokutenAutoFetcher(browser, credentials=credentials)

    if service_id == "mobile":
        from .official_sites import WebBillingAutoFetcher

        return WebBillingAutoFetcher(browser, credentials=credentials)

    raise KeyError(f"unknown receipt service: {service_id}")
