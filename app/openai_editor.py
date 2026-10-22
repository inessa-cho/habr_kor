from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable

from openai import OpenAI

log = logging.getLogger("habr-openai-editor")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
BATCH_SIZE = max(1, int(os.getenv("OPENAI_BATCH_SIZE", "8")))
MAX_RETRIES = max(1, int(os.getenv("OPENAI_MAX_RETRIES", "3")))
TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "120"))

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    if _client is None:
        _client = OpenAI(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)
    return _client


def _schema(expected_ids: list[int]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(expected_ids),
                "maxItems": len(expected_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "enum": expected_ids},
                        "final": {"type": "string"},
                    },
                    "required": ["id", "final"],
                },
            }
        },
        "required": ["items"],
    }


SYSTEM_PROMPT = """당신은 러시아어 기술 문서를 한국어로 옮기는 전문 번역 편집자다.
각 항목에는 러시아어 원문(source)과 MADLAD 초벌 한국어 번역(draft)이 있다.
원문을 기준으로 draft의 오역, 누락, 러시아어식 어순, 깨진 조사와 문장 연결을 고쳐 자연스럽고 정확한 한국어로 완성하라.
정보를 요약·삭제·추가하지 말고, 숫자·코드·제품명·고유명사·링크는 보존하라.
HTML이나 마크다운을 추가하지 말고, 설명·서문·분석·사고 과정도 출력하지 마라.
각 id마다 final 하나만 반환하라."""


def _call_once(items: list[dict]) -> dict[int, str]:
    expected_ids = [int(item["id"]) for item in items]
    response = get_client().responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=json.dumps({"items": items}, ensure_ascii=False),
        temperature=0,
        text={
            "format": {
                "type": "json_schema",
                "name": "translation_edits",
                "strict": True,
                "schema": _schema(expected_ids),
            }
        },
    )
    payload = json.loads(response.output_text)
    output = {int(row["id"]): str(row["final"]) for row in payload["items"]}
    if set(output) != set(expected_ids):
        raise RuntimeError("OpenAI 응답의 항목 ID가 입력과 일치하지 않습니다.")
    if any(not output[item_id].strip() for item_id in expected_ids):
        raise RuntimeError("OpenAI 응답에 빈 번역이 포함되었습니다.")
    return output


def refine_batch(items: list[dict]) -> tuple[dict[int, str], set[int]]:
    """Return successful edits and IDs that must fall back to MADLAD.

    A failed multi-item request is recursively split. A single item that still
    fails after retries falls back without stopping the whole article.
    """
    if not items:
        return {}, set()

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _call_once(items), set()
        except Exception as exc:  # network, schema, refusal, timeout, etc.
            last_error = exc
            log.warning(
                "OpenAI edit attempt %s/%s failed for %s item(s): %s",
                attempt,
                MAX_RETRIES,
                len(items),
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 4))

    if len(items) == 1:
        item_id = int(items[0]["id"])
        log.error("OpenAI edit permanently failed for node %s: %s", item_id, last_error)
        return {}, {item_id}

    middle = len(items) // 2
    left_ok, left_fail = refine_batch(items[:middle])
    right_ok, right_fail = refine_batch(items[middle:])
    return {**left_ok, **right_ok}, left_fail | right_fail


def chunks(rows: Iterable, size: int = BATCH_SIZE):
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
