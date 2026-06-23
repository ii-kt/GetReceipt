from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
LEDGER_PATH = DATA_DIR / "receipt_index.csv"

RECEIPT_DRIVE_FOLDER_ID = "1jwaMMK-KGIyUampBWOjRIY3BULuj6W-M"
RECEIPT_DRIVE_FOLDER_URL = f"https://drive.google.com/drive/folders/{RECEIPT_DRIVE_FOLDER_ID}"
TARGET_MONTH_START = (2026, 1)
DISPLAY_FUTURE_MONTHS = 2


@dataclass(frozen=True)
class ServiceDefinition:
    id: str
    label: str
    default_partner: str
    portal_url: str


SERVICES = (
    ServiceDefinition("epos", "\u5bb6\u8cc3", "\u682a\u5f0f\u4f1a\u793e\u30a8\u30dd\u30b9\u30ab\u30fc\u30c9", "https://www.eposcard.co.jp/memberservice/pc/nocardusedetail/menu_preload.do"),
    ServiceDefinition("commufa", "Wi-Fi", "\u4e2d\u90e8\u30c6\u30ec\u30b3\u30df\u30e5\u30cb\u30b1\u30fc\u30b7\u30e7\u30f3\u682a\u5f0f\u4f1a\u793e", "https://mypage.commufa.jp/join/s/"),
    ServiceDefinition("tokuten", "\u96fb\u6c17", "\u30d5\u30e9\u30c3\u30c8\u30a8\u30ca\u30b8\u30fc\u682a\u5f0f\u4f1a\u793e", "https://outlook.live.com/mail/0/"),
    ServiceDefinition("mobile", "\u643a\u5e2f", "\u682a\u5f0f\u4f1a\u793eNTT\u30c9\u30b3\u30e2", "https://webbilling.ntt-finance.co.jp/mem/b0201/init"),
)


def service_by_id(service_id: str) -> ServiceDefinition:
    for service in SERVICES:
        if service.id == service_id:
            return service
    raise KeyError(f"unknown service: {service_id}")


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_label(year_month: str) -> str:
    year, month = parse_month_key(year_month)
    return f"{year}\u5e74{month}\u6708\u5206"


def parse_month_key(value: str) -> tuple[int, int]:
    year_text, month_text = str(value).split("-", 1)
    year = int(year_text)
    month = int(month_text)
    if month < 1 or month > 12:
        raise ValueError(f"invalid month: {value}")
    return year, month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def selectable_months(today: date | None = None) -> list[str]:
    current = today or date.today()
    start_year, start_month = TARGET_MONTH_START
    end_year, end_month = shift_month(current.year, current.month, DISPLAY_FUTURE_MONTHS)
    months: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append(month_key(year, month))
        year, month = shift_month(year, month, 1)
    return months
