"""Triagem da foto de perfil.

Camada determinística, no mesmo espírito de app/services/file_guard.py: o
arquivo é aceito pelo que ele é nos primeiros bytes, não pelo que o navegador
diz que ele é. Content-Type e extensão vêm do cliente e não valem nada.

O que ela faz:
  - limite de tamanho (AVATAR_MAX_BYTES, 1 MB por padrão);
  - allowlist de formato por magic bytes: PNG, JPEG e WebP;
  - recusa SVG explicitamente. SVG é XML: aceita <script> e vira XSS servido
    do nosso próprio domínio, o que anularia boa parte da CSP.

O que ela NÃO faz: reescrever a imagem. Sem Pillow no projeto, o arquivo é
guardado como veio — inclusive os metadados EXIF, que podem carregar
geolocalização. Por isso a foto só é servida para quem está autenticado.
"""

from __future__ import annotations

DEFAULT_MAX_BYTES = 1024 * 1024

# (assinatura, offset, mime). WebP tem "RIFF" nos bytes 0-3 e "WEBP" nos 8-11.
MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"WEBP", 8, "image/webp"),
)

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def max_bytes() -> int:
    try:
        from flask import current_app

        return int(current_app.config.get("AVATAR_MAX_BYTES", DEFAULT_MAX_BYTES))
    except Exception:
        return DEFAULT_MAX_BYTES


def sniff(data: bytes) -> str | None:
    """Devolve o mime real da imagem ou None se não for um formato aceito."""
    for assinatura, offset, mime in MAGIC:
        if data[offset : offset + len(assinatura)] == assinatura:
            if mime == "image/webp" and data[:4] != b"RIFF":
                continue
            return mime
    return None


def validate(data: bytes) -> tuple[str | None, str | None]:
    """Valida os bytes recebidos.

    Retorna (mime, erro). Exatamente um dos dois vem preenchido.
    """
    if not data:
        return None, "Nenhum arquivo foi enviado."
    limite = max_bytes()
    if len(data) > limite:
        return None, f"A imagem excede o limite de {limite // 1024} KB."
    if data[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:512].lower():
        return None, "SVG não é aceito como foto de perfil. Envie PNG, JPEG ou WebP."
    mime = sniff(data)
    if not mime:
        return None, "Formato não reconhecido. Envie PNG, JPEG ou WebP."
    return mime, None
