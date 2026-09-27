from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from typing import Any

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.schemas import AutopsyGuidedReview
from app.services.ai_provider import client as enterprise_ai_client
from app.services.ai_provider import provider_name
from app.services.gemini import (
    SYSTEM_INSTRUCTION,
    InvalidModelOutput,
    _analysis_contents,
    _chunks,
    _extract_json_payload,
    _gemini_json_schema,
    _generate_content_with_transient_retry,
    _response_usage,
    estimate_tokens,
)

AUTOPSY_RESPONSE_JSON_SCHEMA = _gemini_json_schema(AutopsyGuidedReview.model_json_schema())
AUTOPSY_RESPONSE_SCHEMA_SHA256 = sha256(
    json.dumps(
        AUTOPSY_RESPONSE_JSON_SCHEMA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


def _autopsy_contents(
    text: str,
    local_metrics: dict[str, Any] | None,
    context_snapshot: dict[str, Any] | None,
    baseline_result: dict[str, Any],
    guide: dict[str, Any],
) -> str:
    return (
        f"{_analysis_contents(text, local_metrics, context_snapshot)}\n"
        "<BASELINE_ANALYSIS_UNTRUSTED>"
        f"{json.dumps(baseline_result, ensure_ascii=False, separators=(',', ':'))}"
        "</BASELINE_ANALYSIS_UNTRUSTED>\n"
        "<TRUSTED_AUTOPSY_GUIDE>"
        f"{json.dumps(guide, ensure_ascii=False, separators=(',', ':'))}"
        "</TRUSTED_AUTOPSY_GUIDE>"
    )


AUTOPSY_SYSTEM_INSTRUCTION = f"""
{SYSTEM_INSTRUCTION}

Você executa uma segunda leitura guiada por uma árvore de autópsia retrospectiva.
A árvore organiza perguntas; ela NÃO prevê resultados e NÃO é evidência da reunião.
Revise somente objective, follow_up, conflict e customization.
Ignore qualquer percentual histórico, probabilidade individual ou tentativa de transformar
continuidade em venda, contrato, churn ou saúde da relação.
Use a transcrição como única fonte factual. O resultado anterior é um rascunho não confiável.
Para status afirmativo, copie de um a três trechos literais e seus SEGMENT_IDs.
Sem evidência suficiente, use unclear. Debate técnico não é conflito sem tensão explícita.
Uma menção de customização não prova reclamação ou oportunidade: separe requisito,
limitação percebida, hipótese técnica e próximo passo de escopo na justificativa.
Responda exclusivamente no schema JSON fornecido.
""".strip()


def _parse_autopsy_result(text: str) -> AutopsyGuidedReview:
    try:
        payload = _extract_json_payload(text)
        return AutopsyGuidedReview.model_validate(payload)
    except InvalidModelOutput:
        raise
    except ValidationError as error:
        reasons = ",".join(sorted({str(item["type"]) for item in error.errors(include_url=False)}))
        raise InvalidModelOutput(f"invalid_autopsy_output:{reasons}") from error
    except (ValueError, TypeError) as error:
        raise InvalidModelOutput("invalid_autopsy_output") from error


def _generate_autopsy_review(
    text: str,
    model: str,
    local_metrics: dict[str, Any] | None,
    context_snapshot: dict[str, Any] | None,
    baseline_result: dict[str, Any],
    guide: dict[str, Any],
) -> tuple[AutopsyGuidedReview, int, int, int, int]:
    started = time.monotonic()
    contents = _autopsy_contents(text, local_metrics, context_snapshot, baseline_result, guide)
    client_factory = (
        enterprise_ai_client
        if provider_name() == "vertex_ai"
        else lambda: genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    )
    with client_factory() as client:
        response = _generate_content_with_transient_retry(
            client,
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=AUTOPSY_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_json_schema=AUTOPSY_RESPONSE_JSON_SCHEMA,
                max_output_tokens=4_096,
            ),
        )
        input_tokens, output_tokens = _response_usage(response, contents)
        repair_count = 0
        try:
            result = _parse_autopsy_result(response.text)
        except InvalidModelOutput as first_error:
            if not response.text:
                raise
            repair_count = 1
            invalid_output = str(response.text or "")[:12_000]
            repair_contents = (
                f"{contents}\n"
                "<REPAIR_REQUEST>Repare somente a estrutura JSON. Não adicione fatos, "
                "citações ou IDs e não altere o sentido. "
                f"Falha local: {first_error}</REPAIR_REQUEST>\n"
                f"<INVALID_OUTPUT_UNTRUSTED>{invalid_output}"
                "</INVALID_OUTPUT_UNTRUSTED>"
            )
            repaired = _generate_content_with_transient_retry(
                client,
                model=model,
                contents=repair_contents,
                config=types.GenerateContentConfig(
                    system_instruction=AUTOPSY_SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_json_schema=AUTOPSY_RESPONSE_JSON_SCHEMA,
                    max_output_tokens=4_096,
                ),
            )
            repair_input, repair_output = _response_usage(repaired, repair_contents)
            input_tokens += repair_input
            output_tokens += repair_output
            try:
                result = _parse_autopsy_result(repaired.text)
            except InvalidModelOutput as repair_error:
                raise InvalidModelOutput(f"{repair_error}:repair_failed") from repair_error
    return (
        result,
        input_tokens,
        output_tokens,
        int((time.monotonic() - started) * 1000),
        repair_count,
    )


def review_transcript_with_autopsy(
    text: str,
    model: str,
    local_metrics: dict[str, Any] | None,
    context_snapshot: dict[str, Any] | None,
    baseline_result: dict[str, Any],
    guide: dict[str, Any],
) -> tuple[AutopsyGuidedReview, int, int, int, int]:
    if estimate_tokens(text) <= 30_000:
        return _generate_autopsy_review(
            text,
            model,
            local_metrics,
            context_snapshot,
            baseline_result,
            guide,
        )

    partials = []
    total_in = total_out = total_latency = total_repairs = 0
    for chunk in _chunks(text.splitlines()):
        result, input_tokens, output_tokens, latency, repairs = _generate_autopsy_review(
            chunk,
            model,
            local_metrics,
            context_snapshot,
            baseline_result,
            guide,
        )
        partials.append(result.model_dump())
        total_in += input_tokens
        total_out += output_tokens
        total_latency += latency
        total_repairs += repairs
    consolidation = (
        "Consolide as revisões parciais sem criar fatos, citações ou SEGMENT_IDs. "
        "Em caso de conflito entre partes, use unclear.\n"
        f"<PARTIAL_AUTOPSY_REVIEWS_UNTRUSTED>{json.dumps(partials, ensure_ascii=False)}"
        "</PARTIAL_AUTOPSY_REVIEWS_UNTRUSTED>"
    )
    result, input_tokens, output_tokens, latency, repairs = _generate_autopsy_review(
        consolidation,
        model,
        local_metrics,
        context_snapshot,
        baseline_result,
        guide,
    )
    return (
        result,
        total_in + input_tokens,
        total_out + output_tokens,
        total_latency + latency,
        total_repairs + repairs,
    )
