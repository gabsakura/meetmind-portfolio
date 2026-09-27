from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

SAFE_OBJECT_KEY = re.compile(r"^[a-f0-9]{32}\.bin$")


class SubmissionStorageError(RuntimeError):
    pass


def _fernet() -> Fernet:
    configured = str(current_app.config.get("SUBMISSION_ENCRYPTION_KEY", "")).strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as error:
            raise SubmissionStorageError(
                "SUBMISSION_ENCRYPTION_KEY não é uma chave Fernet válida."
            ) from error
    # Conveniência local/teste. O Compose exige uma chave explícita.
    secret = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(derived)


def _root() -> Path:
    root = Path(str(current_app.config["SUBMISSION_STORAGE_DIR"]))
    if not root.is_absolute():
        root = Path(current_app.root_path).parent / root
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _path(object_key: str) -> Path:
    if not SAFE_OBJECT_KEY.fullmatch(object_key):
        raise SubmissionStorageError("Identificador de arquivo inválido.")
    root = _root()
    target = (root / object_key).resolve()
    if target.parent != root:
        raise SubmissionStorageError("Destino fora do armazenamento privado.")
    return target


def _backend() -> str:
    return str(current_app.config.get("SUBMISSION_STORAGE_BACKEND", "filesystem")).lower()


def _gcs_blob(object_key: str):
    if not SAFE_OBJECT_KEY.fullmatch(object_key):
        raise SubmissionStorageError("Identificador de arquivo inválido.")
    bucket_name = str(current_app.config.get("GCS_SUBMISSION_BUCKET", "")).strip()
    if not bucket_name:
        raise SubmissionStorageError("GCS_SUBMISSION_BUCKET não configurado.")
    try:
        from google.cloud import storage
    except ImportError as error:
        raise SubmissionStorageError("google-cloud-storage não instalado.") from error
    prefix = str(current_app.config.get("GCS_SUBMISSION_PREFIX", "")).strip("/")
    name = f"{prefix}/{object_key}" if prefix else object_key
    bucket = storage.Client().bucket(bucket_name)
    return bucket.blob(
        name,
        kms_key_name=str(current_app.config.get("GCS_KMS_KEY_NAME", "")).strip() or None,
    )


def store(raw: bytes) -> str:
    object_key = f"{uuid.uuid4().hex}.bin"
    encrypted = _fernet().encrypt(raw)
    if _backend() == "gcs":
        try:
            _gcs_blob(object_key).upload_from_string(
                encrypted,
                content_type="application/octet-stream",
            )
        except Exception as error:
            raise SubmissionStorageError("Falha ao gravar no armazenamento de objetos.") from error
        return object_key
    if _backend() != "filesystem":
        raise SubmissionStorageError("Backend de armazenamento desconhecido.")
    target = _path(object_key)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(target)
    return object_key


def load(object_key: str) -> bytes:
    try:
        if _backend() not in {"filesystem", "gcs"}:
            raise SubmissionStorageError("Backend de armazenamento desconhecido.")
        encrypted = (
            _gcs_blob(object_key).download_as_bytes()
            if _backend() == "gcs"
            else _path(object_key).read_bytes()
        )
        return _fernet().decrypt(encrypted)
    except (OSError, InvalidToken) as error:
        raise SubmissionStorageError(
            "O arquivo original não pôde ser recuperado."
        ) from error


def delete(object_key: str | None) -> bool:
    if not object_key:
        return False
    if _backend() == "gcs":
        try:
            blob = _gcs_blob(object_key)
            if not blob.exists():
                return False
            blob.delete()
            return True
        except Exception as error:
            raise SubmissionStorageError("Falha ao remover do armazenamento de objetos.") from error
    if _backend() != "filesystem":
        raise SubmissionStorageError("Backend de armazenamento desconhecido.")
    target = _path(object_key)
    if not target.exists():
        return False
    target.unlink()
    return True
