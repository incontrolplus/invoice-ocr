"""Disk-based caching of OCR tokens and preprocessing artifacts."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from .constants import DEFAULT_OCR_CACHE_DIR, DEFAULT_OCR_LANG, MIN_CONFIDENCE
from .models import OcrToken, PageImage
from .ocr_passes import build_raw_ocr_evidence

logger = logging.getLogger("invoice_ocr")

def compute_file_sha256(file_path: Path | str) -> str:
    """Compute SHA-256 hex digest of a file in streaming 64KB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_ocr_cache_path(
    file_path: Path | str,
    cache_dir: Path | str = DEFAULT_OCR_CACHE_DIR,
) -> Path:
    """Derive deterministic cache path for a file based on its SHA-256 hash."""
    file_hash = compute_file_sha256(file_path)
    return Path(cache_dir) / f"{file_hash}.json"


def load_ocr_cache(cache_path: Path | str) -> tuple[dict[str, Any], list[OcrToken]] | None:
    """Load cached raw OCR evidence and reconstruct OcrToken instances.

    Returns (raw_evidence, tokens) on cache hit, or None on cache miss / corruption.
    """
    path = Path(cache_path)
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict) or "tokens" not in data:
            return None

        tokens: list[OcrToken] = []
        for t in data.get("tokens", []):
            bx = t.get("bbox", [0, 0, 0, 0])
            conf = float(t.get("conf", 0.0))
            is_low = t.get("is_low_confidence", conf < MIN_CONFIDENCE)
            tokens.append(OcrToken(
                text=t.get("text", ""),
                conf=conf,
                bbox=(int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3])),
                page_number=int(t.get("page_number", 1)),
                is_low_confidence=is_low,
                block_num=int(t.get("block_num", 0)),
                par_num=int(t.get("par_num", 0)),
                line_num=int(t.get("line_num", 0)),
                word_num=int(t.get("word_num", 0)),
            ))

        raw_evidence = data.get("raw_evidence")
        if not raw_evidence:
            pages_meta = [
                PageImage(
                    page_number=int(p.get("page_number", 1)),
                    image=None,
                    width=int(p.get("width", 0)),
                    height=int(p.get("height", 0)),
                )
                for p in data.get("pages", [])
            ]
            raw_evidence = build_raw_ocr_evidence(pages_meta, tokens)

        return raw_evidence, tokens
    except Exception as exc:
        logger.warning("Corrupted or unreadable OCR cache at %s: %s", path, exc)
        return None


def save_ocr_cache(
    cache_path: Path | str,
    file_path: Path | str,
    raw_evidence: dict[str, Any],
    tokens: list[OcrToken],
    lang: str = DEFAULT_OCR_LANG,
) -> None:
    """Atomically save raw OCR evidence and token stream to cache."""
    target = Path(cache_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        serialized_tokens = [
            {
                "text": t.text,
                "conf": float(t.conf),
                "bbox": [t.left, t.top, t.width, t.height],
                "page_number": t.page_number,
                "is_low_confidence": t.is_low_confidence,
                "block_num": t.block_num,
                "par_num": t.par_num,
                "line_num": t.line_num,
                "word_num": t.word_num,
            }
            for t in tokens
        ]
        pages_list = [
            {
                "page_number": p.get("page_number", 1),
                "width": p.get("width", 0),
                "height": p.get("height", 0),
            }
            for p in raw_evidence.get("pages", [])
        ]
        payload = {
            "version": 1,
            "created_at": time.time(),
            "source_file": Path(file_path).name,
            "lang": lang,
            "pages": pages_list,
            "tokens": serialized_tokens,
            "raw_evidence": raw_evidence,
        }
        # Atomic write with unique temp file to prevent race conditions during parallel processing
        tmp_file = target.with_name(f"{target.name}.tmp.{uuid.uuid4().hex}")
        tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(target)
    except Exception as exc:
        logger.warning("Failed to save OCR cache to %s: %s", target, exc)


def clear_ocr_cache(cache_dir: Path | str = DEFAULT_OCR_CACHE_DIR) -> int:
    """Remove all cached OCR token files from cache directory. Returns count of deleted files."""
    cdir = Path(cache_dir)
    if not cdir.is_dir():
        return 0
    deleted = 0
    for p in cdir.glob("*.json"):
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def get_ocr_cache_stats(cache_dir: Path | str = DEFAULT_OCR_CACHE_DIR) -> dict[str, Any]:
    """Return summary statistics for the OCR token cache directory."""
    cdir = Path(cache_dir)
    if not cdir.is_dir():
        return {
            "cache_dir": str(cdir),
            "exists": False,
            "total_cached_files": 0,
            "total_size_bytes": 0,
        }
    files = list(cdir.glob("*.json"))
    total_size = sum(f.stat().st_size for f in files if f.is_file())
    return {
        "cache_dir": str(cdir),
        "exists": True,
        "total_cached_files": len(files),
        "total_size_bytes": total_size,
    }


