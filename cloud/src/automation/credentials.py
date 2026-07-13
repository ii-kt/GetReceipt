from __future__ import annotations

from typing import Any, Mapping


CREDENTIAL_SECTIONS: dict[str, tuple[str, ...]] = {
    "epos": ("epos",),
    "commufa": ("commufa",),
    "tokuten": ("tokuten", "outlook", "microsoft"),
    "mobile": ("webbilling", "mobile", "d_account"),
}

LOGIN_ID_KEYS = (
    "login_id",
    "loginId",
    "user_id",
    "userId",
    "username",
    "email",
    "mail",
    "account",
    "account_id",
    "d_account_id",
    "dAccountId",
    "id",
)

PASSWORD_KEYS = ("password", "pass")


def _value(section: Any, keys: tuple[str, ...]) -> str:
    for key in keys:
        try:
            value = section.get(key)
        except (AttributeError, TypeError):
            value = None
        if value:
            return str(value).strip()
    return ""


def service_credentials(secrets: Mapping[str, Any], service_id: str) -> dict[str, str]:
    for section_name in CREDENTIAL_SECTIONS.get(service_id, (service_id,)):
        try:
            section = secrets[section_name]
        except (KeyError, TypeError):
            continue

        login_id = _value(section, LOGIN_ID_KEYS)
        password = _value(section, PASSWORD_KEYS)
        return {
            "login_id": login_id,
            "id": login_id,
            "email": login_id,
            "dAccountId": login_id,
            "password": password,
        }
    return {}


def credentials_configured(secrets: Mapping[str, Any], service_id: str) -> bool:
    credentials = service_credentials(secrets, service_id)
    return bool(credentials.get("login_id") and credentials.get("password"))
