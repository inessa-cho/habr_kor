from __future__ import annotations

import gc
import logging
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)

# Preserve URLs, email addresses, paths, version-like values, and Latin technical terms.
# Habr articles routinely mix Russian prose with product names and API identifiers;
# translating those tokens usually makes the result worse.
PROTECTED_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"
    r"|(?<!\w)/(?:[\w.@+~-]+/)*[\w.@+~-]+"
    r"|\b(?:v?\d+(?:\.\d+){1,4}(?:[-+._][A-Za-z0-9]+)?)\b"
    r"|\b[A-Za-z][A-Za-z0-9_+.#/-]*(?:\s+[A-Za-z][A-Za-z0-9_+.#/-]*)*\b"
)
PLACEHOLDER_RE = re.compile(r"⟦PH(\d{4})⟧")

MODEL_NAME = os.getenv("MADLAD_MODEL", "google/madlad400-3b-mt")
MODEL_ROOT = Path(os.getenv("MADLAD_MODEL_DIR", "/models/madlad400-3b-mt-int8"))
TARGET_PREFIX = os.getenv("MADLAD_TARGET_PREFIX", "<2ko>")
BEAM_SIZE = int(os.getenv("MADLAD_BEAM_SIZE", "4"))
MAX_INPUT_TOKENS = int(os.getenv("MADLAD_MAX_INPUT_TOKENS", "900"))
MAX_OUTPUT_TOKENS = int(os.getenv("MADLAD_MAX_OUTPUT_TOKENS", "768"))
CPU_THREADS = int(os.getenv("MADLAD_CPU_THREADS", "0"))

_model_lock = threading.Lock()
log = logging.getLogger("habr-translator")


def contains_russian(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))


def should_translate_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not contains_russian(stripped):
        return False
    residue = URL_RE.sub("", stripped).strip(" \t\r\n.,;:()[]{}<>—–-")
    return bool(residue and contains_russian(residue))


def _ensure_converted_model() -> None:
    marker = MODEL_ROOT / "model.bin"
    if marker.exists():
        return

    MODEL_ROOT.parent.mkdir(parents=True, exist_ok=True)
    log.warning(
        "MADLAD 모델이 없습니다. %s를 내려받아 CTranslate2 int8 형식으로 변환합니다. "
        "첫 실행에는 매우 오래 걸릴 수 있습니다.",
        MODEL_NAME,
    )

    import ctranslate2

    converter = ctranslate2.converters.TransformersConverter(
        MODEL_NAME,
        low_cpu_mem_usage=True,
    )
    converter.convert(str(MODEL_ROOT), quantization="int8", force=False)
    log.warning("MADLAD 모델 변환 완료: %s", MODEL_ROOT)


@lru_cache(maxsize=1)
def get_engine():
    _ensure_converted_model()

    import ctranslate2
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    translator = ctranslate2.Translator(
        str(MODEL_ROOT),
        device="cpu",
        compute_type="int8",
        inter_threads=1,
        intra_threads=CPU_THREADS,
    )
    return translator, tokenizer


def _protect_terms(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        # Purely lowercase one-letter Latin fragments are usually punctuation noise.
        if len(token) == 1 and token.islower():
            return token
        index = len(protected)
        protected.append(token)
        return f"⟦PH{index:04d}⟧"

    return PROTECTED_RE.sub(replace, text), protected


def _restore_terms(text: str, protected: list[str]) -> str:
    # MADLAD normally copies placeholders exactly. Accept optional whitespace just in case.
    for index, original in enumerate(protected):
        exact = f"⟦PH{index:04d}⟧"
        text = text.replace(exact, original)
        text = re.sub(
            rf"⟦\s*PH\s*0*{index}\s*⟧",
            lambda _m, value=original: value,
            text,
            flags=re.I,
        )
    return text


def _split_for_model(text: str, tokenizer) -> list[str]:
    """Split oversized nodes at sentence/whitespace boundaries without losing content."""
    if len(tokenizer.encode(TARGET_PREFIX + text, add_special_tokens=True)) <= MAX_INPUT_TOKENS:
        return [text]

    pieces = re.split(r"(?<=[.!?…])(?=\s+)|(?<=\n)(?=\S)", text)
    chunks: list[str] = []
    current = ""

    for piece in pieces:
        candidate = current + piece
        if current and len(tokenizer.encode(TARGET_PREFIX + candidate, add_special_tokens=True)) > MAX_INPUT_TOKENS:
            chunks.append(current)
            current = piece
        else:
            current = candidate

    if current:
        chunks.append(current)

    # Last-resort split for a single sentence that is still too long.
    final: list[str] = []
    for chunk in chunks:
        if len(tokenizer.encode(TARGET_PREFIX + chunk, add_special_tokens=True)) <= MAX_INPUT_TOKENS:
            final.append(chunk)
            continue
        words = re.split(r"(\s+)", chunk)
        part = ""
        for word in words:
            candidate = part + word
            if part and len(tokenizer.encode(TARGET_PREFIX + candidate, add_special_tokens=True)) > MAX_INPUT_TOKENS:
                final.append(part)
                part = word
            else:
                part = candidate
        if part:
            final.append(part)
    return final


def _translate_core(text: str) -> str:
    translator, tokenizer = get_engine()
    masked, protected = _protect_terms(text)
    chunks = _split_for_model(masked, tokenizer)
    translated_chunks: list[str] = []

    for chunk in chunks:
        input_text = TARGET_PREFIX + chunk
        token_ids = tokenizer.encode(input_text, add_special_tokens=True)
        source_tokens = tokenizer.convert_ids_to_tokens(token_ids)
        result = translator.translate_batch(
            [source_tokens],
            beam_size=BEAM_SIZE,
            max_input_length=MAX_INPUT_TOKENS,
            max_decoding_length=MAX_OUTPUT_TOKENS,
            repetition_penalty=1.05,
            no_repeat_ngram_size=3,
        )[0]
        output_ids = tokenizer.convert_tokens_to_ids(result.hypotheses[0])
        output = tokenizer.decode(output_ids, skip_special_tokens=True)
        translated_chunks.append(output)

    return _restore_terms("".join(translated_chunks), protected)


def translate_text(text: str) -> str:
    if not should_translate_text(text):
        return text

    prefix = text[: len(text) - len(text.lstrip())]
    suffix = text[len(text.rstrip()) :]
    core = text.strip()

    with _model_lock:
        translated = _translate_core(core)
    return prefix + translated + suffix


def unload_engine() -> None:
    """Release MADLAD resources when the worker stops or encounters an error."""
    with _model_lock:
        get_engine.cache_clear()
        gc.collect()
