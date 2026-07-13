from __future__ import annotations

from datetime import date

from ..config import parse_month_key


def date_in_target_month(target_month: str, candidate: date | None) -> date:
    """Keep a detected date only when it belongs to the requested month.

    A provider PDF without a usable date still needs a deterministic name that
    Drive can classify. In that case the first day acts only as the month key.
    """

    year, month = parse_month_key(target_month)
    if candidate is not None and (candidate.year, candidate.month) == (year, month):
        return candidate
    return date(year, month, 1)
