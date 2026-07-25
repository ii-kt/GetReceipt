from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken

from ..runtime_security import ensure_private_directory, secure_sqlite_files


MICROSOFT_SCOPES = (
    "openid",
    "profile",
    "offline_access",
    "https://graph.microsoft.com/Mail.Read",
)
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{8,200}$")
# PKCE verifiers and our own state use the unreserved set only.
_OAUTH_VALUE = re.compile(r"^[A-Za-z0-9._~-]{20,2048}$")
# A provider's authorization code is opaque: RFC 6749 allows any VSCHAR
# (printable ASCII), and Microsoft codes really do contain characters such
# as "!" and "*". Rejecting them here made every real callback fail with
# MICROSOFT_OAUTH_RESPONSE_INVALID.
_OAUTH_CODE = re.compile(r"^[\x20-\x7E]{20,4096}$")


class MicrosoftOAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MicrosoftOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str
    tenant: str = "consumers"
    authorization_ttl_seconds: int = 10 * 60

    def __post_init__(self) -> None:
        client_id = str(self.client_id or "").strip()
        client_secret = str(self.client_secret or "").strip()
        redirect_uri = str(self.redirect_uri or "").strip()
        encryption_key = str(self.encryption_key or "").strip()
        tenant = str(self.tenant or "consumers").strip()
        parsed = urlsplit(redirect_uri)
        if not _CLIENT_ID.fullmatch(client_id):
            raise ValueError("Microsoft client_idの形式が不正です。")
        if len(client_secret) < 16:
            raise ValueError("Microsoft client_secretが短すぎます。")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("Microsoft redirect_uriはHTTPSで設定してください。")
        if tenant not in {"common", "consumers", "organizations"} and not _CLIENT_ID.fullmatch(tenant):
            raise ValueError("Microsoft tenantの形式が不正です。")
        if self.authorization_ttl_seconds < 60 or self.authorization_ttl_seconds > 30 * 60:
            raise ValueError("Microsoft authorization TTLの範囲が不正です。")
        try:
            Fernet(encryption_key.encode("ascii"))
        except Exception as error:
            raise ValueError("GETRECEIPT_TOKEN_ENCRYPTION_KEYの形式が不正です。") from error
        object.__setattr__(self, "client_id", client_id)
        object.__setattr__(self, "client_secret", client_secret)
        object.__setattr__(self, "redirect_uri", redirect_uri)
        object.__setattr__(self, "encryption_key", encryption_key)
        object.__setattr__(self, "tenant", tenant)


class MicrosoftTokenStore:
    def __init__(
        self,
        *,
        database_path: Path,
        owner_id: str,
        encryption_key: str,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.owner_id = str(owner_id or "").strip()
        self._fernet = Fernet(str(encryption_key).encode("ascii"))
        if not self.owner_id:
            raise ValueError("owner_id is required")
        ensure_private_directory(self.database_path.parent)
        self._initialize()
        secure_sqlite_files(self.database_path)

    def connected(self) -> bool:
        encrypted = self._encrypted_refresh_token()
        if encrypted is None:
            return False
        try:
            value = self._fernet.decrypt(encrypted).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            self.delete()
            return False
        if len(value) < 20:
            self.delete()
            return False
        return True

    def updated_at(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM oauth_tokens WHERE provider = ? AND owner = ?",
                ("microsoft", self.owner_id),
            ).fetchone()
        return str(row[0]) if row else ""

    def save_refresh_token(self, refresh_token: str) -> None:
        value = str(refresh_token or "")
        if len(value) < 20:
            raise MicrosoftOAuthError(
                "Microsoftの長期認証情報を確認できませんでした。",
                code="MICROSOFT_REFRESH_TOKEN_MISSING",
            )
        encrypted = self._fernet.encrypt(value.encode("utf-8"))
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_tokens(provider, owner, encrypted_refresh_token, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, owner) DO UPDATE SET
                    encrypted_refresh_token = excluded.encrypted_refresh_token,
                    updated_at = excluded.updated_at
                """,
                ("microsoft", self.owner_id, encrypted, timestamp),
            )
            connection.commit()
        value = ""

    def load_refresh_token(self) -> str:
        encrypted = self._encrypted_refresh_token()
        if encrypted is None:
            raise MicrosoftOAuthError(
                "Microsoftメールが未接続です。",
                code="MICROSOFT_OAUTH_REQUIRED",
            )
        try:
            return self._fernet.decrypt(encrypted).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as error:
            self.delete()
            raise MicrosoftOAuthError(
                "Microsoft認証情報を復号できません。再接続してください。",
                code="MICROSOFT_TOKEN_DECRYPT_FAILED",
            ) from error

    def save_pending_authorization(
        self,
        *,
        state: str,
        code_verifier: str,
        expires_at: datetime,
    ) -> None:
        normalized_state = str(state or "").strip()
        normalized_verifier = str(code_verifier or "").strip()
        if (
            not _OAUTH_VALUE.fullmatch(normalized_state)
            or not _OAUTH_VALUE.fullmatch(normalized_verifier)
        ):
            raise ValueError("OAuth pending authorizationの形式が不正です。")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        state_hash = _state_hash(normalized_state)
        encrypted_verifier = self._fernet.encrypt(normalized_verifier.encode("ascii"))
        now = datetime.now(timezone.utc)
        expires = expires_at.astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM oauth_pending_authorizations
                    WHERE provider = ? AND owner = ? AND expires_at <= ?
                    """,
                    ("microsoft", self.owner_id, now.isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO oauth_pending_authorizations(
                        provider,
                        owner,
                        state_hash,
                        encrypted_code_verifier,
                        expires_at,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, owner, state_hash) DO UPDATE SET
                        encrypted_code_verifier = excluded.encrypted_code_verifier,
                        expires_at = excluded.expires_at,
                        created_at = excluded.created_at
                    """,
                    (
                        "microsoft",
                        self.owner_id,
                        state_hash,
                        encrypted_verifier,
                        expires.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def consume_pending_authorization(self, *, state: str) -> str | None:
        normalized_state = str(state or "").strip()
        if not _OAUTH_VALUE.fullmatch(normalized_state):
            return None
        state_hash = _state_hash(normalized_state)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT encrypted_code_verifier, expires_at
                    FROM oauth_pending_authorizations
                    WHERE provider = ? AND owner = ? AND state_hash = ?
                    """,
                    ("microsoft", self.owner_id, state_hash),
                ).fetchone()
                connection.execute(
                    """
                    DELETE FROM oauth_pending_authorizations
                    WHERE provider = ? AND owner = ? AND state_hash = ?
                    """,
                    ("microsoft", self.owner_id, state_hash),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(str(row[1]))
            if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
                return None
            verifier = self._fernet.decrypt(bytes(row[0])).decode("ascii")
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
            return None
        return verifier if _OAUTH_VALUE.fullmatch(verifier) else None

    def delete(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM oauth_tokens WHERE provider = ? AND owner = ?",
                ("microsoft", self.owner_id),
            )
            connection.commit()

    def _encrypted_refresh_token(self) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                (
                    "SELECT encrypted_refresh_token FROM oauth_tokens "
                    "WHERE provider = ? AND owner = ?"
                ),
                ("microsoft", self.owner_id),
            ).fetchone()
        return bytes(row[0]) if row else None

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_tokens(
                    provider TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    encrypted_refresh_token BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, owner)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_pending_authorizations(
                    provider TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    encrypted_code_verifier BLOB NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(provider, owner, state_hash)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_oauth_pending_expiry
                ON oauth_pending_authorizations(provider, owner, expires_at)
                """
            )
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            yield connection
        finally:
            connection.close()


class MicrosoftOAuthManager:
    def __init__(
        self,
        *,
        config: MicrosoftOAuthConfig,
        token_store: MicrosoftTokenStore,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.token_store = token_store
        self.session = session or requests.Session()

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "connected": self.token_store.connected(),
            "updated_at": self.token_store.updated_at(),
        }

    def start(self) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        expires_at = now + timedelta(seconds=self.config.authorization_ttl_seconds)
        self.token_store.save_pending_authorization(
            state=state,
            code_verifier=verifier,
            expires_at=expires_at,
        )
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "response_mode": "query",
                "scope": " ".join(MICROSOFT_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return {
            "authorization_url": (
                f"https://login.microsoftonline.com/{self.config.tenant}"
                f"/oauth2/v2.0/authorize?{query}"
            ),
            "expires_at": expires_at.isoformat(),
        }

    def complete(self, *, code: str, state: str) -> dict[str, Any]:
        normalized_code = str(code or "").strip()
        normalized_state = str(state or "").strip()
        if not _OAUTH_CODE.fullmatch(normalized_code) or not _OAUTH_VALUE.fullmatch(normalized_state):
            raise MicrosoftOAuthError(
                "Microsoft認証応答の形式が不正です。",
                code="MICROSOFT_OAUTH_RESPONSE_INVALID",
            )
        code_verifier = self.token_store.consume_pending_authorization(
            state=normalized_state
        )
        if code_verifier is None:
            raise MicrosoftOAuthError(
                "Microsoft接続の有効期限が切れました。最初からやり直してください。",
                code="MICROSOFT_OAUTH_STATE_EXPIRED",
            )
        payload = self._token_request(
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "authorization_code",
                "code": normalized_code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": code_verifier,
                "scope": " ".join(MICROSOFT_SCOPES),
            }
        )
        refresh_token = str(payload.get("refresh_token") or "")
        self.token_store.save_refresh_token(refresh_token)
        normalized_code = ""
        refresh_token = ""
        return self.status()

    def access_token(self) -> str:
        refresh_token = self.token_store.load_refresh_token()
        try:
            try:
                payload = self._token_request(
                    {
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "scope": " ".join(MICROSOFT_SCOPES),
                    }
                )
            except MicrosoftOAuthError as error:
                if error.code == "MICROSOFT_OAUTH_RECONNECT_REQUIRED":
                    self.token_store.delete()
                raise
        finally:
            refresh_token = ""
        access_token = str(payload.get("access_token") or "")
        if len(access_token) < 20:
            raise MicrosoftOAuthError(
                "Microsoftアクセストークンを取得できませんでした。",
                code="MICROSOFT_ACCESS_TOKEN_MISSING",
            )
        rotated = str(payload.get("refresh_token") or "")
        if rotated:
            self.token_store.save_refresh_token(rotated)
            rotated = ""
        return access_token

    def disconnect(self) -> None:
        self.token_store.delete()

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        url = (
            f"https://login.microsoftonline.com/{self.config.tenant}"
            "/oauth2/v2.0/token"
        )
        try:
            response = self.session.post(
                url,
                data=form,
                headers={"Accept": "application/json"},
                timeout=20,
            )
        except requests.RequestException as error:
            raise MicrosoftOAuthError(
                "Microsoft認証サーバーへ接続できませんでした。",
                code="MICROSOFT_OAUTH_UNREACHABLE",
            ) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400 or not isinstance(payload, dict):
            oauth_error = (
                str(payload.get("error") or "").strip().lower()
                if isinstance(payload, dict)
                else ""
            )
            # Keep Microsoft's own diagnosis: without it every failure looks
            # identical and the actual misconfiguration stays invisible.
            description = ""
            if isinstance(payload, dict):
                description = str(payload.get("error_description") or "").strip()
            description = description.splitlines()[0][:200] if description else ""
            suffix = f"（{oauth_error or response.status_code}: {description}）" if description else ""
            if oauth_error in {"invalid_grant", "interaction_required"}:
                raise MicrosoftOAuthError(
                    f"Microsoft接続の更新が必要です。再接続してください。{suffix}",
                    code="MICROSOFT_OAUTH_RECONNECT_REQUIRED",
                )
            raise MicrosoftOAuthError(
                f"Microsoft認証を完了できませんでした。再接続してください。{suffix}",
                code="MICROSOFT_OAUTH_REJECTED",
            )
        return payload


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()
