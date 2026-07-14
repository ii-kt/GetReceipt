from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"

RECEIPT_DRIVE_FOLDER_ID = "1jwaMMK-KGIyUampBWOjRIY3BULuj6W-M"
RECEIPT_DRIVE_FOLDER_URL = f"https://drive.google.com/drive/folders/{RECEIPT_DRIVE_FOLDER_ID}"
TARGET_MONTH_START = (2026, 1)
TOKYO = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class ServiceDefinition:
    id: str
    label: str
    default_partner: str
    portal_url: str
    partner_aliases: tuple[str, ...]
    transaction_month_offset: int
    accepts_yenless_amount: bool = False


SERVICES = (
    ServiceDefinition(
        id="epos",
        label="家賃",
        default_partner="株式会社エポスカード",
        portal_url="https://www.eposcard.co.jp/memberservice/pc/nocardusedetail/menu_preload.do",
        partner_aliases=("株式会社エポスカード",),
        transaction_month_offset=-1,
    ),
    ServiceDefinition(
        id="commufa",
        label="Wi-Fi",
        default_partner="中部テレコミュニケーション株式会社",
        portal_url="https://mypage.commufa.jp/join/s/",
        partner_aliases=("中部テレコミュニケーション株式会社",),
        transaction_month_offset=1,
        accepts_yenless_amount=True,
    ),
    ServiceDefinition(
        id="tokuten",
        label="電気",
        default_partner="フラットエナジー株式会社",
        portal_url="https://outlook.live.com/mail/0/",
        partner_aliases=("フラットエナジー株式会社",),
        transaction_month_offset=1,
    ),
    ServiceDefinition(
        id="mobile",
        label="携帯",
        default_partner="NTTファイナンス株式会社",
        portal_url="https://webbilling.ntt-finance.co.jp/mem/b0201/init",
        partner_aliases=(
            "NTTファイナンス株式会社",
            "株式会社NTTファイナンス",
            "株式会社NTTドコモ",
        ),
        transaction_month_offset=0,
    ),
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
    return f"{year}年{month}月分"


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


def expected_transaction_month(service_id: str, usage_month: str) -> str:
    """Map a service usage month to the month encoded in its Drive filename."""

    service = service_by_id(service_id)
    year, month = parse_month_key(usage_month)
    year, month = shift_month(year, month, service.transaction_month_offset)
    return month_key(year, month)


def usage_month_for_transaction(service_id: str, transaction_month: str) -> str:
    """Map a Drive transaction month back to the service usage month."""

    service = service_by_id(service_id)
    year, month = parse_month_key(transaction_month)
    year, month = shift_month(year, month, -service.transaction_month_offset)
    return month_key(year, month)


def selectable_months(today: date | None = None) -> list[str]:
    current = today or datetime.now(TOKYO).date()
    year, month = TARGET_MONTH_START
    months: list[str] = []
    while (year, month) <= (current.year, current.month):
        months.append(month_key(year, month))
        year, month = shift_month(year, month, 1)
    return months
