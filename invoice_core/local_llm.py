"""100% Local LLM & AI Integration Module (Ollama / LocalAI / On-Premise).

Replaces all paid external AI providers (Anthropic Claude, Google Vertex AI, OpenAI)
with 100% private, self-hosted, offline-capable local AI models.

Supported local backends:
1. Ollama (default: http://localhost:11434)
   - Vision models: llama3.2-vision, minicpm-v, llava
   - Text/Post-processing models: qwen2.5:7b, llama3.1:8b, mistral:7b
2. Rule-Based Fallback (Zero-dependency):
   - When Ollama is not installed or running, seamlessly falls back to
     local deterministic heuristics without throwing errors or halting the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("invoice_ocr.local_llm")

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_LOCAL_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:7b")
DEFAULT_VISION_MODEL = os.environ.get("LOCAL_VISION_MODEL", "llama3.2-vision")
LOCAL_LLM_ENABLED = os.environ.get("ENABLE_LOCAL_LLM", "1").lower() in ("1", "true", "yes")


def is_ollama_available(host: str = DEFAULT_OLLAMA_HOST, timeout: float = 1.0) -> bool:
    """Check if local Ollama daemon is reachable on localhost."""
    if not LOCAL_LLM_ENABLED:
        return False
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{host.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


def get_available_local_models(host: str = DEFAULT_OLLAMA_HOST, timeout: float = 2.0) -> list[str]:
    """Retrieve list of locally installed Ollama models."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{host.rstrip('/')}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as exc:
        logger.debug("Failed to list local Ollama models: %s", exc)
    return []


def query_local_llm(
    prompt: str,
    system_prompt: str | None = None,
    image_base64: str | None = None,
    model: str | None = None,
    host: str = DEFAULT_OLLAMA_HOST,
    timeout: float = 30.0,
) -> str | None:
    """Send an extraction or correction query to local Ollama.

    Args:
        prompt: User instruction or OCR text.
        system_prompt: Optional system prompt instructing JSON schema output.
        image_base64: Optional base64-encoded image for local multimodal models.
        model: Model name (defaults to DEFAULT_VISION_MODEL if image given, else DEFAULT_LOCAL_MODEL).
        host: Ollama server URL (default http://localhost:11434).
        timeout: HTTP request timeout in seconds.

    Returns:
        Generated text/JSON string from local model, or None if unavailable.
    """
    selected_model = model or (DEFAULT_VISION_MODEL if image_base64 else DEFAULT_LOCAL_MODEL)
    url = f"{host.rstrip('/')}/api/generate"

    payload: dict[str, Any] = {
        "model": selected_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.0,
        },
    }

    if system_prompt:
        payload["system"] = system_prompt

    if image_base64:
        # Strip data URL prefix if present
        clean_b64 = image_base64.split(",")[-1].strip()
        payload["images"] = [clean_b64]

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                result = resp.json()
                return result.get("response", "").strip()
            logger.warning("Local Ollama returned status %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.debug("Local Ollama query skipped or failed (%s). Continuing with rule-based engine.", exc)

    return None


def correct_ocr_text_with_local_ai(
    raw_ocr_text: str,
    model: str | None = None,
    host: str = DEFAULT_OLLAMA_HOST,
) -> tuple[str, bool]:
    """Correct noisy OCR text using local AI if available, else return original.

    Returns:
        (text, is_ai_corrected)
    """
    if not raw_ocr_text or not raw_ocr_text.strip():
        return raw_ocr_text, False

    if not is_ollama_available(host):
        return raw_ocr_text, False

    prompt = (
        "Correct clear Bulgarian OCR typos and Cyrillic/Latin character confusion "
        "in the following invoice text while strictly preserving all original numbers, dates, "
        "and layout structure. Do not add conversational text or markdown explanation:\n\n"
        f"{raw_ocr_text}"
    )

    corrected = query_local_llm(
        prompt=prompt,
        system_prompt="You are a Bulgarian invoice OCR post-processor. Return only the corrected raw text.",
        model=model,
        host=host,
        timeout=15.0,
    )

    if corrected and len(corrected) > 20:
        return corrected, True

    return raw_ocr_text, False
