from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping, Sequence
from typing import Any


class AccessControlConfigError(RuntimeError):
    pass


def require_owner_access(st: Any, secrets: Mapping[str, Any]) -> None:
    """Require an allowlisted OIDC identity before any receipt data is read.

    A development instance with no configured receipt data remains available.
    As soon as Drive credentials or a remote worker are configured, access
    control becomes fail-closed so a public Streamlit URL cannot read the
    owner's receipts or exercise the owner's worker credentials.
    """

    access = _section(secrets, "app_access")
    sensitive_configured = any(
        _section_present(secrets, name)
        for name in (
            "receipt_worker",
            "google_service_account",
            "epos",
            "commufa",
            "tokuten",
            "outlook",
            "microsoft",
            "webbilling",
            "mobile",
            "d_account",
        )
    ) or any(
        str(os.getenv(name) or "").strip()
        for name in (
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GETRECEIPT_PROVIDER_CREDENTIALS_JSON",
        )
    )
    if access is None:
        if sensitive_configured:
            _fatal(
                st,
                "所有者ログインが未設定です",
                "Streamlit Secretsに[app_access]と[auth]を設定するまで、"
                "領収書データと取得ワーカーにはアクセスできません。",
                "OWNER_AUTH_NOT_CONFIGURED",
            )
        return

    mode = str(_value(access, "mode") or "oidc").strip().lower()
    if mode != "oidc":
        _fatal(
            st,
            "所有者ログインの設定が不正です",
            "本番のapp_access.modeはoidcにしてください。",
            "OWNER_AUTH_MODE_INVALID",
        )

    allowed_emails = {
        item.casefold()
        for item in _string_sequence(_value(access, "allowed_emails"))
    }
    allowed_subjects = set(_string_sequence(_value(access, "allowed_subjects")))
    allowed_issuers = set(_string_sequence(_value(access, "allowed_issuers")))
    allowed_identities = set(
        _string_sequence(_value(access, "allowed_identities"))
    )
    allowed_identity_hashes = {
        item.casefold()
        for item in _string_sequence(_value(access, "allowed_identity_hashes"))
    }
    if (
        not allowed_emails
        and not allowed_subjects
        and not allowed_identities
        and not allowed_identity_hashes
    ):
        _fatal(
            st,
            "所有者allowlistが未設定です",
            "[app_access]にallowed_identity_hashes/allowed_identities、"
            "またはissuerと組み合わせたallowed_emails/allowed_subjectsを"
            "設定してください。",
            "OWNER_ALLOWLIST_EMPTY",
        )
    if (allowed_emails or allowed_subjects) and not allowed_issuers:
        _fatal(
            st,
            "OIDC issuer allowlistが未設定です",
            "メールまたはsubjectで許可する場合はallowed_issuersも設定してください。",
            "OWNER_ISSUER_ALLOWLIST_EMPTY",
        )

    user = getattr(st, "user", None)
    if user is None or not bool(getattr(user, "is_logged_in", False)):
        st.title("GetReceipt")
        st.caption("領収書と取得用Google Chromeを保護するため、所有者ログインが必要です。")
        provider = str(_value(access, "provider") or "").strip()
        if provider:
            st.button(
                "所有者としてログイン",
                type="primary",
                use_container_width=True,
                on_click=st.login,
                args=(provider,),
            )
        else:
            st.button(
                "所有者としてログイン",
                type="primary",
                use_container_width=True,
                on_click=st.login,
            )
        st.stop()
        return

    email = str(_user_value(user, "email") or "").strip().casefold()
    subject = str(_user_value(user, "sub") or "").strip()
    issuer = str(_user_value(user, "iss") or "").strip().rstrip("/")
    email_verified = _user_value(user, "email_verified") is True
    issuer_allowed = any(
        hmac.compare_digest(issuer.encode(), candidate.rstrip("/").encode())
        for candidate in allowed_issuers
    )
    identity = f"{issuer}|{subject}"
    identity_hash = (
        "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if issuer and subject
        else ""
    )
    identity_allowed = bool(issuer and subject) and any(
        hmac.compare_digest(identity.encode(), candidate.encode())
        for candidate in allowed_identities
    )
    identity_hash_allowed = bool(identity_hash) and any(
        hmac.compare_digest(identity_hash.encode(), candidate.encode())
        for candidate in allowed_identity_hashes
    )
    email_allowed = any(
        hmac.compare_digest(email.encode(), candidate.encode())
        for candidate in allowed_emails
    ) and issuer_allowed and email_verified
    subject_allowed = any(
        hmac.compare_digest(subject.encode(), candidate.encode())
        for candidate in allowed_subjects
    ) and issuer_allowed
    if not (
        identity_allowed
        or identity_hash_allowed
        or email_allowed
        or subject_allowed
    ):
        st.error(
            "このアカウントはGetReceiptの所有者として許可されていません。",
            icon=":material/lock:",
        )
        if identity_hash:
            st.caption(
                "初回設定中の所有者は、次の値をStreamlit Secretsの"
                "[app_access].allowed_identity_hashesへ登録してください。"
                "この値だけではログインできません。"
            )
            st.code(identity_hash, language=None)
        st.button(
            "ログアウト",
            use_container_width=True,
            on_click=st.logout,
        )
        st.stop()


def _fatal(st: Any, title: str, detail: str, code: str) -> None:
    st.error(f"{title}\n\n{detail}\n\n`{code}`", icon=":material/lock:")
    st.stop()
    raise AccessControlConfigError(code)


def _section(secrets: Mapping[str, Any], name: str) -> Any | None:
    try:
        section = secrets[name]
    except (KeyError, TypeError):
        return None
    except Exception as error:
        if type(error).__name__ == "StreamlitSecretNotFoundError":
            return None
        raise
    return section


def _section_present(secrets: Mapping[str, Any], name: str) -> bool:
    section = _section(secrets, name)
    if section is None:
        return False
    try:
        return bool(list(section.keys()))
    except (AttributeError, TypeError):
        return True


def _value(section: Any, name: str) -> Any:
    try:
        return section.get(name)
    except (AttributeError, TypeError):
        return None


def _string_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(
        normalized
        for item in value
        if (normalized := str(item or "").strip())
    )


def _user_value(user: Any, name: str) -> Any:
    try:
        return user.get(name)
    except (AttributeError, TypeError):
        return getattr(user, name, "")
