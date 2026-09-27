from __future__ import annotations

import re
import unicodedata
from hashlib import sha256
from typing import Any

from app.services.privacy import detect_pii


def max_bytes() -> int:
    """Limite de tamanho alinhado ao protocolo de submissão."""
    try:
        from flask import current_app

        return int(current_app.config.get("MAX_SUBMISSION_BYTES", DEFAULT_MAX_BYTES))
    except Exception:
        return DEFAULT_MAX_BYTES

"""Triagem estrutural de arquivos de transcrição.

Camada DETERMINÍSTICA: roda localmente, não consome tokens e não depende de
modelo de linguagem. É esta camada que efetivamente barra arquivo perigoso.

O que ela faz:
  - allowlist de extensão e limite de tamanho;
  - rejeita binário disfarçado de texto (magic bytes de executável, arquivo
    compactado, PDF, documento OLE com macro);
  - rejeita caracteres de controle e bytes nulos;
  - detecta caracteres bidirecionais e de largura zero (ataque Trojan Source,
    CVE-2021-42574), que escondem texto do revisor humano;
  - detecta injeção de fórmula de planilha (=cmd, +, -, @ no início de célula);
  - detecta tentativa de prompt injection — o risco REAL deste pipeline, já que
    o conteúdo será enviado a um LLM adiante;
  - detecta PII residual reaproveitando app.services.privacy.

O que ela NÃO faz: varredura de assinatura de malware. Para isso existe o
ClamAV opcional em app/services/antivirus.py — nenhum LLM substitui antivírus.
"""

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
# Trecho analisado pelos sinais heurísticos. Para decidir "isto parece uma
# transcrição?" não é preciso varrer o arquivo inteiro; amostra do início e do
# fim dá o mesmo resultado por uma fração do custo.
SIGNAL_SAMPLE_BYTES = 2 * 1024 * 1024
# Janela das varreduras de conteúdo ativo e prompt injection.
DEEP_SCAN_WINDOW = 4 * 1024 * 1024
ALLOWED_EXTENSIONS = (".txt", ".md", ".vtt", ".srt", ".json")
MIN_CHARS = 200

# Assinaturas de arquivo que nunca deveriam aparecer em transcrição de texto.
MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "executável Windows (PE)"),
    (b"\x7fELF", "executável Linux (ELF)"),
    (b"\xca\xfe\xba\xbe", "executável Mach-O/Java"),
    (b"PK\x03\x04", "arquivo compactado ZIP (inclui docx/xlsx)"),
    (b"Rar!", "arquivo compactado RAR"),
    (b"7z\xbc\xaf\x27\x1c", "arquivo compactado 7z"),
    (b"\x1f\x8b", "arquivo compactado GZIP"),
    (b"%PDF", "documento PDF"),
    (b"\xd0\xcf\x11\xe0", "documento OLE legado (pode conter macro VBA)"),
    (b"\xff\xd8\xff", "imagem JPEG"),
    (b"\x89PNG", "imagem PNG"),
    (b"GIF8", "imagem GIF"),
    (b"\x00\x00\x01\x00", "ícone/executável ICO"),
    (b"#!", "script com shebang"),
)

# Indícios de macro/objeto ativo, caso o texto venha de export de documento.
ACTIVE_CONTENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bAuto(?:Open|Close|Exec)\b", re.I),
        "macro de execução automática",
    ),
    (re.compile(r"\bShell\s*\(", re.I), "chamada Shell()"),
    (re.compile(r"\bWScript\.Shell\b", re.I), "WScript.Shell"),
    (
        re.compile(r"\bpowershell\b.{0,40}\b-enc(?:odedcommand)?\b", re.I),
        "PowerShell codificado",
    ),
    (re.compile(r"<script\b", re.I), "tag <script>"),
    (re.compile(r"\bdocument\.cookie\b", re.I), "acesso a cookie"),
    (re.compile(r"javascript:", re.I), "URI javascript:"),
    (re.compile(r"\beval\s*\(", re.I), "eval()"),
    (re.compile(r"\bbase64_decode\s*\(", re.I), "base64_decode()"),
)

# Tentativas de subverter as instruções do modelo. Vão para o veredito como
# alerta; o conteúdo segue tratado como não confiável no prompt.
PROMPT_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bignor[ea]\s+(?:as\s+)?(?:instru|todas)", re.I),
        "pedido para ignorar instruções",
    ),
    (
        re.compile(r"\bignore\s+(?:all\s+)?(?:previous|above)\b", re.I),
        "ignore previous instructions",
    ),
    (
        re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|prior)\b", re.I),
        "disregard previous",
    ),
    (
        re.compile(
            r"\b(?:você|voce)\s+(?:agora\s+)?é\s+(?:um|uma)\b"
            r".{0,40}\b(?:assistente|IA|sistema)\b",
            re.I,
        ),
        "tentativa de redefinir o papel do modelo",
    ),
    (re.compile(r"\bsystem\s*prompt\b", re.I), "menção a system prompt"),
    (
        re.compile(
            r"\b(?:reveal|mostre|revele)\s+(?:seu|your)\s+(?:prompt|instru)", re.I
        ),
        "pedido para revelar o prompt",
    ),
    (
        re.compile(r"</?(?:TRANSCRIPT_UNTRUSTED|INVALID_OUTPUT_UNTRUSTED)>", re.I),
        "tentativa de fechar o delimitador de conteúdo não confiável",
    ),
    (re.compile(r"\bdeveloper\s+mode\b", re.I), "developer mode"),
)

# Sinais de que o texto realmente é uma reunião transcrita (heurística local,
# barata; a leitura semântica fica para a IA).
SPEAKER_LINE = re.compile(r"^\s*[\w][\w .'-]{1,40}\s*:\s+\S", re.M)
TIMESTAMP = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\b")
VTT_CUE = re.compile(r"-->")
DIALOG_MARKERS = re.compile(
    r"\b(?:bom dia|boa tarde|boa noite|obrigad[oa]|então|a gente|"
    r"pra gente|tudo bem|reunião|call|alinha(?:r|mento)|combinado)\b",
    re.I,
)

BIDI_CHARS = {
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069", "\u200f", "\u200e",
}
ZERO_WIDTH_CHARS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}

# Buscas em C, não em laço Python: essencial para arquivos grandes.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
BIDI_RE = re.compile("[" + "".join(BIDI_CHARS) + "]")
ZERO_WIDTH_RE = re.compile("[" + "".join(ZERO_WIDTH_CHARS) + "]")

# Tabela de tradução para a sanitização em passada única (também em C).
_REMOVE_TABLE = {ord(char): None for char in BIDI_CHARS | ZERO_WIDTH_CHARS}
_REMOVE_TABLE.update({code: None for code in range(0x00, 0x20) if code not in (9, 10, 13)})
_REMOVE_TABLE.update({code: None for code in range(0x7F, 0xA0)})

FORMULA_INJECTION = re.compile(
    r"^[\s\"']*[=+\-@]\s*(?:cmd|DDE|HYPERLINK|IMPORT|WEBSERVICE)", re.I | re.M
)


def content_hash(text: str) -> str:
    """Hash do conteúdo normalizado.

    A normalização (espaços colapsados, NFC, minúsculas) garante que dois
    envios do mesmo arquivo com diferença apenas de formatação caiam no mesmo
    hash — é o que evita gastar token duas vezes pelo mesmo diagnóstico.
    """
    bruto = text or ""
    # normalize() é caro; em texto ASCII puro é desnecessário.
    normalized = bruto if bruto.isascii() else unicodedata.normalize("NFC", bruto)
    normalized = " ".join(normalized.split()).lower()
    return sha256(normalized.encode("utf-8")).hexdigest()


def has_utf16_bom(raw: bytes) -> bool:
    """Aceita UTF-16 somente quando o arquivo declara a codificação no BOM."""
    return raw.startswith((b"\xff\xfe", b"\xfe\xff"))


def decode_text(raw: bytes) -> tuple[str | None, str | None]:
    """Decodifica texto comum e exports UTF-16 identificados explicitamente."""
    encodings = ("utf-16",) if has_utf16_bom(raw) else ("utf-8-sig", "latin-1")
    for encoding in encodings:
        try:
            return raw.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, "O arquivo não é texto legível em UTF-8, UTF-16 com BOM nem Latin-1."


def _binary_findings(raw: bytes) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    head = raw[:64]
    for signature, label in MAGIC_SIGNATURES:
        if head.startswith(signature):
            findings.append(
                {
                    "code": "binary_signature",
                    "severity": "blocker",
                    "detail": f"Assinatura de {label} no início do arquivo.",
                }
            )
    if b"\x00" in raw[:8192] and not has_utf16_bom(raw):
        findings.append(
            {
                "code": "null_bytes",
                "severity": "blocker",
                "detail": "Bytes nulos encontrados: conteúdo binário, não transcrição.",
            }
        )
    return findings


def _charset_findings(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    if CONTROL_RE.search(text):
        findings.append(
            {
                "code": "control_characters",
                "severity": "blocker",
                "detail": (
                    "Caracteres de controle fora de tabulação e quebra de linha."
                ),
            }
        )

    if BIDI_RE.search(text):
        findings.append(
            {
                "code": "bidi_override",
                "severity": "blocker",
                "detail": (
                    "Caracteres de sobrescrita bidirecional detectados. Podem esconder "
                    "texto do revisor humano (padrão Trojan Source)."
                ),
            }
        )

    if ZERO_WIDTH_RE.search(text):
        findings.append(
            {
                "code": "zero_width",
                "severity": "warning",
                "detail": "Caracteres de largura zero detectados e removidos na sanitização.",
            }
        )

    if FORMULA_INJECTION.search(text):
        findings.append(
            {
                "code": "formula_injection",
                "severity": "warning",
                "detail": (
                    "Linha iniciando com fórmula de planilha. Risco apenas se o conteúdo "
                    "for exportado para CSV/Excel."
                ),
            }
        )
    return findings


def _active_content_findings(text: str) -> list[dict[str, str]]:
    return [
        {
            "code": "active_content",
            "severity": "blocker",
            "detail": f"Indício de conteúdo ativo: {label}.",
        }
        for pattern, label in ACTIVE_CONTENT_PATTERNS
        if pattern.search(text)
    ]


def _injection_findings(text: str) -> list[dict[str, str]]:
    return [
        {
            "code": "prompt_injection",
            "severity": "warning",
            "detail": f"Possível prompt injection: {label}.",
        }
        for pattern, label in PROMPT_INJECTION_PATTERNS
        if pattern.search(text)
    ]


def _janela(text: str) -> str:
    """Início + fim do texto, para as varreduras de custo alto."""
    if len(text) <= DEEP_SCAN_WINDOW:
        return text
    metade = DEEP_SCAN_WINDOW // 2
    return text[:metade] + "\n" + text[-metade:]


def sanitize(text: str) -> str:
    """Remove o que não deve chegar ao banco nem ao modelo."""
    bruto = text or ""
    cleaned = bruto if bruto.isascii() else unicodedata.normalize("NFC", bruto)
    return cleaned.translate(_REMOVE_TABLE).strip()


def transcript_signals(text: str, full_length: int | None = None) -> dict[str, Any]:
    """Heurística local de "cara de transcrição", sem custo de token.

    Em arquivos grandes a contagem roda sobre uma amostra (início + fim) e o
    veredito usa densidade, não valor absoluto — o resultado é o mesmo e o custo
    não cresce com o tamanho do arquivo.
    """
    total = full_length if full_length is not None else len(text)
    amostrado = len(text) < total
    speakers = len(SPEAKER_LINE.findall(text))
    timestamps = len(TIMESTAMP.findall(text))
    cues = len(VTT_CUE.findall(text))
    dialog = len(DIALOG_MARKERS.findall(text))
    words = len(text.split())

    score = 0
    if speakers >= 4:
        score += 2
    elif speakers >= 2:
        score += 1
    if cues >= 2 or timestamps >= 4:
        score += 2
    elif timestamps >= 1:
        score += 1
    if dialog >= 3:
        score += 2
    elif dialog >= 1:
        score += 1
    if words >= 300:
        score += 1

    return {
        "speaker_lines": speakers,
        "timestamps": timestamps,
        "caption_cues": cues,
        "dialog_markers": dialog,
        "words": words,
        "sampled": amostrado,
        "sample_chars": len(text) if amostrado else None,
        "total_chars": total,
        "local_score": score,
        "local_verdict": (
            "provável transcrição" if score >= 4
            else "indefinido" if score >= 2
            else "improvável"
        ),
    }


def screen(filename: str, raw: bytes) -> dict[str, Any]:
    """Executa a triagem completa.

    Retorna sempre a mesma estrutura, com `blocked` indicando se o arquivo deve
    ser recusado antes de qualquer chamada de IA.
    """
    findings: list[dict[str, str]] = []
    name = (filename or "").strip().lower()

    if not name.endswith(ALLOWED_EXTENSIONS):
        findings.append(
            {
                "code": "extension_not_allowed",
                "severity": "blocker",
                "detail": f"Extensão fora da lista permitida ({', '.join(ALLOWED_EXTENSIONS)}).",
            }
        )
    limite = max_bytes()
    if len(raw) > limite:
        findings.append(
            {
                "code": "file_too_large",
                "severity": "blocker",
                "detail": (
                    f"Arquivo com {len(raw) / 1048576:.1f} MB; "
                    f"o limite é {limite / 1048576:.0f} MB."
                ),
            }
        )
    if not raw:
        findings.append(
            {"code": "empty_file", "severity": "blocker", "detail": "Arquivo vazio."}
        )

    findings.extend(_binary_findings(raw))

    text: str | None = None
    if not any(item["severity"] == "blocker" for item in findings):
        text, decode_error = decode_text(raw)
        if decode_error:
            findings.append(
                {"code": "undecodable", "severity": "blocker", "detail": decode_error}
            )

    signals: dict[str, Any] = {}
    pii: list[str] = []
    clean_text = ""

    if text is not None:
        # Varredura de caractere: arquivo inteiro (regex simples, custo baixo).
        findings.extend(_charset_findings(text))

        # Varreduras profundas: janela limitada, por dois motivos concretos.
        # 1) Prompt injection só tem efeito no trecho que chega ao modelo, e o
        #    classificador lê apenas os primeiros milhares de caracteres.
        # 2) Conteúdo ativo (macro, script) nunca é executado por este sistema:
        #    o arquivo é tratado exclusivamente como texto. A checagem é
        #    higiene, não contenção de execução.
        # Varrer 100 MB atrás disso custaria ~40s por requisição sem ganho real.
        findings.extend(_active_content_findings(_janela(text)))
        findings.extend(_injection_findings(_janela(text)))

        clean_text = sanitize(text)
        if len(clean_text) < MIN_CHARS:
            findings.append(
                {
                    "code": "too_short",
                    "severity": "blocker",
                    "detail": f"Apenas {len(clean_text)} caracteres úteis; mínimo de {MIN_CHARS}.",
                }
            )
        if len(clean_text) > SIGNAL_SAMPLE_BYTES:
            metade = SIGNAL_SAMPLE_BYTES // 2
            amostra = clean_text[:metade] + "\n" + clean_text[-metade:]
        else:
            amostra = clean_text
        signals = transcript_signals(amostra, full_length=len(clean_text))
        pii = detect_pii(amostra)
        if pii:
            findings.append(
                {
                    "code": "pii_detected",
                    "severity": "warning",
                    "detail": (
                        f"Dados pessoais detectados ({', '.join(sorted(pii))}). "
                        "Anonimize antes de aprovar."
                    ),
                }
            )

    blocked = any(item["severity"] == "blocker" for item in findings)
    return {
        "blocked": blocked,
        "findings": findings,
        "signals": signals,
        "pii": sorted(pii),
        "clean_text": "" if blocked else clean_text,
        "content_hash": content_hash(clean_text) if clean_text else content_hash(""),
        "bytes": len(raw),
        "filename": filename,
    }
