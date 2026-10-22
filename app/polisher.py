from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3:4b")
REQUEST_TIMEOUT = float(os.getenv("QWEN_REQUEST_TIMEOUT", "1800"))
MAX_RETRIES = int(os.getenv("QWEN_MAX_RETRIES", "1"))
BATCH_SIZE = max(1, int(os.getenv("QWEN_BATCH_SIZE", "20")))
BATCH_MAX_CHARS = max(2000, int(os.getenv("QWEN_BATCH_MAX_CHARS", "7000")))

log = logging.getLogger("habr-polisher")

PROTECTED_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"
    r"|(?<!\w)/(?:[\w.@+~-]+/)*[\w.@+~-]+"
    r"|\b(?:v?\d+(?:\.\d+){1,4}(?:[-+._][A-Za-z0-9]+)?)\b"
    r"|\b[A-Za-z][A-Za-z0-9_+.#/-]*(?:\s+[A-Za-z][A-Za-z0-9_+.#/-]*)*\b"
)
PLACEHOLDER_RE = re.compile(r"⟦KEEP(\d{6})⟧")

_SYSTEM = """당신은 러시아어 기술 문서를 한국어로 옮긴 초벌 번역을 교정하는 편집자다.
입력은 여러 문장 항목으로 구성된다. 각 항목의 의미를 그대로 유지하면서 자연스럽고 읽기 좋은 한국어로 다듬어라.

반드시 지킬 규칙:
- 원문에 없는 정보, 설명, 추측을 추가하지 않는다.
- 요약하거나 문장을 생략하지 않는다.
- 숫자, 고유명사, 회사명, 제품명, API명, 코드, URL, 버전은 바꾸지 않는다.
- ⟦KEEP000000⟧ 같은 보호 표시는 철자와 순서를 정확히 유지한다.
- 항목 수와 id를 절대로 바꾸지 않는다.
- 출력은 설명 없이 JSON 배열 하나만 반환한다.
- 각 원소는 정확히 {\"id\": 정수, \"text\": \"교정문\"} 형식이어야 한다.
"""


@dataclass
class PolishItem:
    id: int
    source: str
    draft: str


@dataclass
class PolishResult:
    id: int
    text: str
    status: str  # done | fallback
    error: str | None = None


def _wait_for_ollama() -> None:
    last_error: Exception | None = None
    for _ in range(90):
        try:
            with httpx.Client(timeout=5) as client:
                response = client.get(f"{OLLAMA_URL}/api/tags")
                response.raise_for_status()
                return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"Ollama가 준비되지 않았습니다: {last_error}")


def ensure_model() -> None:
    _wait_for_ollama()
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{OLLAMA_URL}/api/tags")
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("models", [])}
    if QWEN_MODEL in names or f"{QWEN_MODEL}:latest" in names:
        return

    log.warning("Qwen 후처리 모델을 내려받습니다: %s", QWEN_MODEL)
    with httpx.Client(timeout=None) as client:
        response = client.post(
            f"{OLLAMA_URL}/api/pull",
            json={"model": QWEN_MODEL, "stream": False},
        )
        response.raise_for_status()
    log.warning("Qwen 모델 다운로드 완료: %s", QWEN_MODEL)


def make_batches(items: list[PolishItem]) -> list[list[PolishItem]]:
    batches: list[list[PolishItem]] = []
    current: list[PolishItem] = []
    current_chars = 0
    for item in items:
        item_chars = len(item.source) + len(item.draft)
        if current and (len(current) >= BATCH_SIZE or current_chars + item_chars > BATCH_MAX_CHARS):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _protect_batch(items: list[PolishItem]) -> tuple[list[dict], dict[int, list[tuple[str, str]]]]:
    next_token = 0
    protected_by_id: dict[int, list[tuple[str, str]]] = {}
    payload: list[dict] = []

    for item in items:
        pairs: list[tuple[str, str]] = []

        def repl(match: re.Match[str]) -> str:
            nonlocal next_token
            original = match.group(0)
            if len(original) == 1 and original.islower():
                return original
            token = f"⟦KEEP{next_token:06d}⟧"
            next_token += 1
            pairs.append((token, original))
            return token

        masked = PROTECTED_RE.sub(repl, item.draft)
        protected_by_id[item.id] = pairs
        payload.append({"id": item.id, "source": item.source.strip(), "draft": masked})

    return payload, protected_by_id


def _strip_reasoning(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json|text|markdown)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def _extract_json_array(text: str) -> list[dict]:
    cleaned = _strip_reasoning(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start < 0 or end <= start:
            raise RuntimeError("Qwen 응답에서 JSON 배열을 찾지 못했습니다.")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, list):
        raise RuntimeError("Qwen 응답이 JSON 배열이 아닙니다.")
    return value


def _call_qwen(payload: list[dict]) -> list[dict]:
    prompt = (
        "다음 JSON 배열의 각 항목을 교정하라. source는 러시아어 원문이고 draft는 MADLAD 한국어 초벌 번역이다. "
        "id는 그대로 유지하고 text에 교정문을 넣어 JSON 배열만 출력하라.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={
                        "model": QWEN_MODEL,
                        "system": _SYSTEM,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "think": False,
                        "keep_alive": -1,
                        "options": {
                            "temperature": 0.0,
                            "top_p": 0.8,
                            "num_ctx": 8192,
                            "num_predict": 4096,
                        },
                    },
                )
                response.raise_for_status()
                return _extract_json_array(response.json().get("response", ""))
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_error))


def polish_batch(items: list[PolishItem]) -> list[PolishResult]:
    if not items:
        return []

    payload, protected_by_id = _protect_batch(items)
    source_by_id = {item.id: item for item in items}

    try:
        raw_results = _call_qwen(payload)
    except Exception as exc:
        error = f"배치 호출 실패: {exc}"
        log.warning("Qwen batch fallback (%d items): %s", len(items), error)
        return [PolishResult(item.id, item.draft, "fallback", error) for item in items]

    result_map: dict[int, str] = {}
    for result in raw_results:
        try:
            result_id = int(result["id"])
            text = str(result["text"])
            if result_id in source_by_id and result_id not in result_map:
                result_map[result_id] = text
        except (KeyError, TypeError, ValueError):
            continue

    final: list[PolishResult] = []
    for item in items:
        output = result_map.get(item.id, "").strip()
        pairs = protected_by_id[item.id]
        expected = {token for token, _ in pairs}
        found = {f"⟦KEEP{value}⟧" for value in PLACEHOLDER_RE.findall(output)}

        if not output:
            final.append(PolishResult(item.id, item.draft, "fallback", "Qwen 응답에서 해당 id가 누락되었습니다."))
            continue
        if expected != found:
            final.append(PolishResult(item.id, item.draft, "fallback", "Qwen이 보호된 기술 용어를 누락하거나 변경했습니다."))
            continue

        for token, original in pairs:
            output = output.replace(token, original)
        prefix = item.draft[: len(item.draft) - len(item.draft.lstrip())]
        suffix = item.draft[len(item.draft.rstrip()) :]
        final.append(PolishResult(item.id, prefix + output + suffix, "done"))

    return final


def unload_model() -> None:
    try:
        with httpx.Client(timeout=60) as client:
            client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": QWEN_MODEL, "prompt": "", "stream": False, "keep_alive": 0},
            )
    except Exception as exc:
        log.warning("Qwen 모델 언로드 요청 실패: %s", exc)
