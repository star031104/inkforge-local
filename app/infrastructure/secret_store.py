"""Project-scoped credentials kept outside manuscript and revision storage."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROJECT_SECRET_FIELDS = (
    "api_key",
    "reasoning_api_key",
    "prose_api_key",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretProtectionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class CredentialProtector:
    """Encrypt credentials with Windows DPAPI or a permission-bound local key."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.key_path = database_path.with_suffix(database_path.suffix + ".key")
        self.kind = "dpapi" if os.name == "nt" else "local-key"

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data)
        return (
            _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
            buffer,
        )

    def _dpapi_protect(self, data: bytes) -> bytes:
        source, _buffer = self._blob(data)
        output = _DataBlob()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
        ):
            raise SecretProtectionError("Windows 无法加密模型密钥")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def _dpapi_unprotect(self, data: bytes) -> bytes:
        source, _buffer = self._blob(data)
        output = _DataBlob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
        ):
            raise SecretProtectionError(
                "模型密钥无法解密；它可能来自其他 Windows 用户"
            )
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def _local_key(self) -> bytes:
        if self.key_path.is_file():
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise SecretProtectionError("本机凭据密钥文件已损坏")
            return key
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(
                self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            return self._local_key()
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
        return key

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(
                hmac.new(
                    key,
                    b"inkforge-credential" + nonce + counter.to_bytes(8, "big"),
                    hashlib.sha256,
                ).digest()
            )
            counter += 1
        return bytes(output[:length])

    def protect(self, value: str) -> str:
        raw = value.encode("utf-8")
        if os.name == "nt":
            encrypted = self._dpapi_protect(raw)
            return "enc:dpapi-v1:" + base64.urlsafe_b64encode(encrypted).decode("ascii")
        key = self._local_key()
        nonce = secrets.token_bytes(16)
        stream = self._keystream(key, nonce, len(raw))
        encrypted = bytes(left ^ right for left, right in zip(raw, stream))
        tag = hmac.new(key, b"inkforge-mac" + nonce + encrypted, hashlib.sha256).digest()
        packed = nonce + tag + encrypted
        return "enc:local-v1:" + base64.urlsafe_b64encode(packed).decode("ascii")

    def unprotect(self, stored: str) -> str:
        if stored.startswith("enc:dpapi-v1:"):
            packed = base64.urlsafe_b64decode(stored.split(":", 2)[2].encode("ascii"))
            return self._dpapi_unprotect(packed).decode("utf-8")
        if stored.startswith("enc:local-v1:"):
            packed = base64.urlsafe_b64decode(stored.split(":", 2)[2].encode("ascii"))
            if len(packed) < 48:
                raise SecretProtectionError("本机凭据数据已损坏")
            nonce, tag, encrypted = packed[:16], packed[16:48], packed[48:]
            key = self._local_key()
            expected = hmac.new(
                key, b"inkforge-mac" + nonce + encrypted, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(tag, expected):
                raise SecretProtectionError("本机凭据校验失败")
            stream = self._keystream(key, nonce, len(encrypted))
            return bytes(
                left ^ right for left, right in zip(encrypted, stream)
            ).decode("utf-8")
        # Pre-0.32 stores used plaintext. The constructor immediately rewrites it.
        return stored


def extract_project_secrets(project: dict[str, Any]) -> dict[str, str]:
    settings = project.get("settings") if isinstance(project.get("settings"), dict) else {}
    result = {
        f"settings.{field}": str(settings.get(field) or "").strip()
        for field in PROJECT_SECRET_FIELDS
    }
    research = settings.get("research") if isinstance(settings.get("research"), dict) else {}
    result["settings.research.brave_api_key"] = str(
        research.get("brave_api_key") or ""
    ).strip()
    routes = settings.get("role_routes") if isinstance(settings.get("role_routes"), dict) else {}
    for role, route in routes.items():
        if isinstance(route, dict):
            result[f"settings.role_routes.{role}.api_key"] = str(
                route.get("api_key") or ""
            ).strip()
    return {key: value for key, value in result.items() if value}


def strip_project_secrets(project: dict[str, Any]) -> dict[str, Any]:
    """Clear credentials in-place while preserving the stable project schema."""
    settings = project.get("settings") if isinstance(project.get("settings"), dict) else None
    if settings is None:
        return project
    for field in PROJECT_SECRET_FIELDS:
        if field in settings:
            settings[field] = ""
    research = settings.get("research")
    if isinstance(research, dict) and "brave_api_key" in research:
        research["brave_api_key"] = ""
    routes = settings.get("role_routes")
    if isinstance(routes, dict):
        for route in routes.values():
            if isinstance(route, dict) and "api_key" in route:
                route["api_key"] = ""
    return project


def apply_project_secrets(
    project: dict[str, Any], secrets: dict[str, str]
) -> dict[str, Any]:
    settings = project.setdefault("settings", {})
    if not isinstance(settings, dict):
        return project
    for field in PROJECT_SECRET_FIELDS:
        value = secrets.get(f"settings.{field}", "")
        if value:
            settings[field] = value
    research = settings.setdefault("research", {})
    if isinstance(research, dict):
        value = secrets.get("settings.research.brave_api_key", "")
        if value:
            research["brave_api_key"] = value
    routes = settings.setdefault("role_routes", {})
    if isinstance(routes, dict):
        prefix = "settings.role_routes."
        suffix = ".api_key"
        for key, value in secrets.items():
            if key.startswith(prefix) and key.endswith(suffix) and value:
                role = key[len(prefix) : -len(suffix)]
                route = routes.setdefault(role, {})
                if isinstance(route, dict):
                    route["api_key"] = value
    return project


class ProjectSecretStore:
    """Small isolated SQLite store intentionally excluded from project backups."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.protector = CredentialProtector(path)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS project_secrets (
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, name)
                )
                """
            )
            rows = db.execute("SELECT project_id, name, value FROM project_secrets").fetchall()
            for project_id, name, value in rows:
                stored = str(value or "")
                if stored and not stored.startswith("enc:"):
                    db.execute(
                        "UPDATE project_secrets SET value = ?, updated_at = ? "
                        "WHERE project_id = ? AND name = ?",
                        (self.protector.protect(stored), _utc_now(), project_id, name),
                    )
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, project_id: str) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT name, value FROM project_secrets WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {
            str(name): self.protector.unprotect(str(value))
            for name, value in rows
        }

    def replace(self, project_id: str, values: dict[str, str]) -> None:
        clean = {str(key): str(value) for key, value in values.items() if str(value)}
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM project_secrets WHERE project_id = ?", (project_id,))
            now = _utc_now()
            db.executemany(
                "INSERT INTO project_secrets(project_id, name, value, updated_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (project_id, key, self.protector.protect(value), now)
                    for key, value in clean.items()
                ],
            )

    def merge(self, project_id: str, values: dict[str, str]) -> None:
        clean = {str(key): str(value) for key, value in values.items() if str(value)}
        if not clean:
            return
        with self.lock, self._connect() as db:
            now = _utc_now()
            db.executemany(
                "INSERT INTO project_secrets(project_id, name, value, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, name) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                [
                    (project_id, key, self.protector.protect(value), now)
                    for key, value in clean.items()
                ],
            )

    def delete(self, project_id: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM project_secrets WHERE project_id = ?", (project_id,))
