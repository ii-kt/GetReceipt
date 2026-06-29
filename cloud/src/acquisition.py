from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import month_label, parse_month_key, service_by_id


EPOS_PAYMENT_DAY = 27


@dataclass(frozen=True)
class AcquisitionGuidance:
    heading: str
    target_hint: str
    steps: tuple[str, ...]
    note: str


def epos_payment_option_hint(target_month: str) -> str:
    year, month = parse_month_key(target_month)
    return f"{year}年{month}月{EPOS_PAYMENT_DAY}日お支払分"


def default_transaction_date(service_id: str, target_month: str, today: date | None = None) -> date:
    year, month = parse_month_key(target_month)
    if service_id == "epos":
        return date(year, month, EPOS_PAYMENT_DAY)
    return today or date.today()


def acquisition_guidance(service_id: str, target_month: str) -> AcquisitionGuidance:
    service = service_by_id(service_id)
    if service.id == "epos":
        target_hint = epos_payment_option_hint(target_month)
        return AcquisitionGuidance(
            heading="エポスカードの明細画面で選ぶ項目",
            target_hint=target_hint,
            steps=(
                f"プルダウンで「{target_hint}」を選択",
                "取得用ブラウザのログイン後に「PDFを取得してDriveへ保存」を押す",
                "PDFの取得、ファイル名生成、Drive保存までGetReceiptが続けて実行",
            ),
            note="家賃は公式サイト側で対象月を選んでからPDF照会する必要があります。",
        )

    if service.id == "commufa":
        return AcquisitionGuidance(
            heading="コミュファの対象月",
            target_hint=month_label(target_month),
            steps=(
                "取得用ブラウザでMyコミュファにログイン",
                "GetReceiptが請求確認、過去の請求額一覧、対象月の利用明細へ進む",
                "印刷用ページをPDF化し、Driveへ保存",
            ),
            note="対象月の行が未表示の場合は、サイト側で発行前として扱われます。",
        )

    if service.id == "tokuten":
        year, month = parse_month_key(target_month)
        lookup_year, lookup_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return AcquisitionGuidance(
            heading="トクテンでんきの検索対象メール",
            target_hint=f"{lookup_year}年{lookup_month}月の請求メール",
            steps=(
                "取得用ブラウザでOutlook Webにログイン",
                "GetReceiptが対象の請求メールを検索",
                "添付PDFをダウンロードし、Driveへ保存",
            ),
            note="電気は利用月の翌月メールから請求書PDFを取得します。",
        )

    if service.id == "mobile":
        return AcquisitionGuidance(
            heading="Webビリングの証明書対象月",
            target_hint=month_label(target_month),
            steps=(
                "取得用ブラウザでWebビリングまたはdアカウントにログイン",
                "GetReceiptが料金支払証明書の対象月を選択",
                "PDFをダウンロードし、Driveへ保存",
            ),
            note="追加認証やセキュリティコードが出た場合は、取得用ブラウザ上で本人操作を完了してください。",
        )

    return AcquisitionGuidance(
        heading=f"{service.label}の取得手順",
        target_hint=month_label(target_month),
        steps=(
            f"公式サイトまたはメールで「{month_label(target_month)}」の明細を確認",
            "取得用ブラウザでPDFを取得",
            "GetReceiptがDrive保存と台帳更新まで実行",
        ),
        note="追加のサービスを増やした場合も、この取得フローに接続します。",
    )
