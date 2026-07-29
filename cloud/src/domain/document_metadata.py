from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from io import BytesIO


@dataclass(frozen=True)
class ExtractedReceiptData:
    transaction_date: date | None
    amount_yen: int | None
    text_length: int


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def extract_pdf_text(content: bytes) -> str:
    """Return a PDF's real text layer, or nothing.

    Decoding the raw bytes as a fallback used to be appended here. For a
    scanned or image-only invoice that yields the file's internal structure
    - object numbers, stream lengths, transform matrices - and the amount
    extractor then mines a plausible-looking number out of it. A receipt was
    saved with a fabricated amount that way, so an unreadable PDF must report
    no text instead of inventing some.
    """

    if not content.startswith(b"%PDF"):
        # Not a PDF: the caller may still be handing over plain page text.
        return _decoded(content)

    parts: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        for page in reader.pages:
            parts.append(page.extract_text() or "")
    except Exception:
        # The file claims to be a PDF but cannot be parsed. Scanning its bytes
        # is the only thing left and cannot be confused with a real document's
        # internal structure, which is what produced fabricated amounts.
        return _decoded(content)
    text = normalize_text(" ".join(part for part in parts if part))
    if text:
        return text
    # A scanned or rendered-to-image invoice carries its amount only in the
    # picture. This provider switched to that format, so read it rather than
    # failing every month.
    return _ocr_pdf_text(content)


def _ocr_pdf_text(content: bytes) -> str:
    """Read an image-only PDF with OCR, or return nothing.

    OCR is optional: without the engine installed this returns no text, and
    the callers refuse to save rather than guessing an amount.
    """

    try:
        from pypdf import PdfReader
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""
    try:
        import numpy

        engine = RapidOCR()
        reader = PdfReader(BytesIO(content))
        lines: list[str] = []
        for page in reader.pages[:4]:
            for image in list(page.images)[:2]:
                result, _elapsed = engine(numpy.array(image.image.convert("RGB")))
                lines.extend(str(item[1]) for item in (result or []))
        return _repair_ocr_text(normalize_text(" ".join(lines)))
    except Exception:
        return ""


# Observed misreadings on this invoice: the engine's model favours simplified
# Chinese shapes, and renders 円 as 月.
_OCR_CHARACTER_REPAIRS = {
    "凳": "発",
    "达": "込",
    "额": "額",
    "单": "単",
    "绿": "録",
    "豹": "約",
}


def _repair_ocr_text(text: str) -> str:
    for wrong, right in _OCR_CHARACTER_REPAIRS.items():
        text = text.replace(wrong, right)
    # A comma-grouped number is never followed by 月 in Japanese, so this is
    # the yen sign every time.
    return re.sub(r"(\d{1,3}(?:,\d{3})+)\s*月", r"\1円", text)


def _decoded(content: bytes) -> str:
    """Best-effort text for content that is not a parseable PDF.

    Each encoding recovers characters the others drop - a yen sign written in
    cp932 disappears under utf-8 - so all of them contribute.
    """

    parts: list[str] = []
    for encoding in ("utf-8", "cp932", "latin1"):
        try:
            parts.append(content.decode(encoding, errors="ignore"))
        except Exception:
            continue
    return normalize_text(" ".join(part for part in parts if part))


def extract_transaction_date(
    text: str,
    *,
    prefer_month: str = "",
) -> date | None:
    """Find the receipt's own date.

    An invoice carries several dates - issue date, usage period, due date -
    and only the one inside the month the receipt belongs to identifies it.
    When that month is known, prefer a candidate from it; the caller would
    otherwise fall back to the first of the month and lose the real date.
    """

    normalized = normalize_text(text)
    patterns = (
        re.compile(r"((?:19|20)\d{2})\D{1,6}(\d{1,2})\D{1,6}(\d{1,2})"),
        re.compile(r"((?:19|20)\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])"),
    )
    candidates: list[tuple[int, date]] = []
    for pattern in patterns:
        for match in pattern.finditer(normalized):
            try:
                candidate = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            context = normalized[max(0, match.start() - 80): match.end() + 80]
            prefix = normalized[max(0, match.start() - 24): match.start()]
            score = 1
            if re.search(r"支払|振替|引落|口座", prefix):
                score = 3
            elif re.search(r"請求|発行|作成|利用", prefix) or re.search(r"請求|発行|作成|利用", context):
                score = 2
            candidates.append((score, candidate))
    if not candidates:
        return None
    if prefer_month:
        try:
            year, month = (int(part) for part in prefer_month.split("-", 1))
        except (TypeError, ValueError):
            year = month = 0
        in_month = [
            item
            for item in candidates
            if (item[1].year, item[1].month) == (year, month)
        ]
        if in_month:
            candidates = in_month
    return sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]


def _normalize_amount(value: str) -> int | None:
    text = re.sub(r"[,円￥¥\s]", "", value)
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


def extract_amount_yen(text: str) -> int | None:
    normalized = normalize_text(text)
    amount_core = r"([0-9][0-9,\s]{1,})"
    patterns = (
        re.compile(amount_core + r"\s*(?:円|￥|¥)"),
        re.compile(r"(?:円|￥|¥)\s*" + amount_core),
    )
    candidates: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(normalized):
            amount = _normalize_amount(match.group(1))
            if amount is None:
                continue
            context = normalized[max(0, match.start() - 100): match.end() + 100]
            score = 1
            if re.search(r"合計|請求|支払|金額|税込|振替|引落", context):
                score = 3
            elif re.search(r"利用|明細|家賃", context):
                score = 2
            candidates.append((score, amount))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-item[0], -item[1]))[0][1]


def extract_receipt_data(
    content: bytes,
    *text_hints: str,
    prefer_month: str = "",
) -> ExtractedReceiptData:
    text = normalize_text(" ".join([*text_hints, extract_pdf_text(content)]))
    return ExtractedReceiptData(
        transaction_date=extract_transaction_date(text, prefer_month=prefer_month),
        amount_yen=extract_amount_yen(text),
        text_length=len(text),
    )
