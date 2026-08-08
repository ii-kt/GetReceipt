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

# Web billing accepts two identities and the sign-in they lead to is nothing
# alike: a d-account goes out to docomo, which fronts it with a bot check only
# a person can answer; NTT Finance's own ID stays on their site and mails a
# one-time password to the mailbox this app already reads.
#
# Which one the owner registered has to be said, not guessed. Reading it off
# whichever key happened to hold the login id meant a d-account identity was
# reported for every configuration, so the Web billing ID route could never be
# taken no matter what was in Secrets.
D_ACCOUNT_ID_KEYS = ("d_account_id", "dAccountId", "docomo_id")
WEBBILLING_ID_KEYS = ("webbilling_id", "web_billing_id", "webBillingId")

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

        webbilling_id = _value(section, WEBBILLING_ID_KEYS)
        d_account_id = _value(section, D_ACCOUNT_ID_KEYS)
        # An explicitly named identity wins, and the Web billing one wins over
        # a d-account left in place beside it: naming it is how the owner asks
        # for the route that needs nothing from them.
        login_id = webbilling_id or d_account_id or _value(section, LOGIN_ID_KEYS)
        password = _value(section, PASSWORD_KEYS)
        return {
            "login_id": login_id,
            "id": login_id,
            "email": login_id,
            # Only what was actually configured as such. Filled from any login
            # id, this said "d-account" for every setup.
            "dAccountId": d_account_id,
            "d_account_id": d_account_id,
            "webbilling_id": webbilling_id,
            "password": password,
        }
    return {}


def credentials_configured(secrets: Mapping[str, Any], service_id: str) -> bool:
    credentials = service_credentials(secrets, service_id)
    return bool(credentials.get("login_id") and credentials.get("password"))
