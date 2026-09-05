"""Test helpers, reference oracles, and utilities for Bulgarian invoice OCR test suite."""
import hashlib
import inspect
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import numpy as np
import cv2
from PIL import Image, ImageDraw

from invoice_ocr import OcrToken, LogicalLine, TableColumn

ACCEPTANCE_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")
CORPUS_ROOT = Path("/Volumes/NO NAME/_ФАКТУРИ")


def calculate_eik9_checksum(digits8: str) -> int:
    """Authoritative reference oracle for Bulgarian 9-digit EIK check digit (Modulo 11)."""
    d = [int(c) for c in digits8]
    w1 = [1, 2, 3, 4, 5, 6, 7, 8]
    s1 = sum(di * wi for di, wi in zip(d, w1))
    r1 = s1 % 11
    if r1 < 10:
        return r1
    w2 = [3, 4, 5, 6, 7, 8, 9, 10]
    s2 = sum(di * wi for di, wi in zip(d, w2))
    r2 = s2 % 11
    if r2 < 10:
        return r2
    return 0


def calculate_eik13_checksum(digits12: str) -> int:
    """Authoritative reference oracle for Bulgarian 13-digit EIK check digit (Modulo 11)."""
    d = [int(c) for c in digits12]
    sub = d[8:12]
    w1 = [2, 7, 3, 5]
    s1 = sum(di * wi for di, wi in zip(sub, w1))
    r1 = s1 % 11
    if r1 < 10:
        return r1
    w2 = [4, 9, 5, 7]
    s2 = sum(di * wi for di, wi in zip(sub, w2))
    r2 = s2 % 11
    if r2 < 10:
        return r2
    return 0


def calculate_iban_mod97(iban_str: str) -> int:
    """Authoritative reference oracle for ISO 7064 Modulo 97-10 IBAN validation."""
    cleaned = "".join(iban_str.split()).upper()
    if len(cleaned) < 4:
        return -1
    rearranged = cleaned[4:] + cleaned[:4]
    numeric_str = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric_str += ch
        elif "A" <= ch <= "Z":
            numeric_str += str(ord(ch) - ord("A") + 10)
        else:
            return -1
    try:
        return int(numeric_str) % 97
    except ValueError:
        return -1


def create_synthetic_test_image(
    text: str = "ФАКТУРА 0000001234",
    width: int = 800,
    height: int = 200,
    angle: float = 0.0,
    low_contrast: bool = False,
) -> np.ndarray:
    """Create a synthetic test image with Bulgarian text for testing."""
    bg_val = 200 if low_contrast else 255
    fg_val = 150 if low_contrast else 0
    img = Image.new("L", (width, height), color=bg_val)
    draw = ImageDraw.Draw(img)
    draw.text((30, height // 3), text, fill=fg_val)

    if abs(angle) > 0.01:
        img = img.rotate(angle, expand=False, fillcolor=bg_val)

    arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)


class DirectoryStateSnapshot:
    """Snapshot the state of a directory to verify strict read-only compliance."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.file_hashes: dict[str, str] = {}
        self.file_mtimes: dict[str, float] = {}
        self.file_sizes: dict[str, int] = {}
        self.snapshot()

    def snapshot(self) -> None:
        if not self.directory.exists():
            return
        for root, _, files in os.walk(self.directory):
            for file in files:
                full_path = Path(root) / file
                try:
                    stat = full_path.stat()
                    self.file_mtimes[str(full_path)] = stat.st_mtime
                    self.file_sizes[str(full_path)] = stat.st_size
                    with open(full_path, "rb") as f:
                        header = f.read(65536)
                        self.file_hashes[str(full_path)] = hashlib.sha256(header).hexdigest()
                except (PermissionError, FileNotFoundError):
                    pass

    def verify_unchanged(self) -> tuple[bool, list[str]]:
        """Verify that no files were modified, added, or deleted."""
        errors = []
        current_files = set()
        if not self.directory.exists():
            return True, []

        for root, _, files in os.walk(self.directory):
            for file in files:
                full_path = Path(root) / file
                current_files.add(str(full_path))
                path_str = str(full_path)
                if path_str not in self.file_hashes:
                    errors.append(f"New file created on read-only volume: {path_str}")
                    continue
                stat = full_path.stat()
                if stat.st_size != self.file_sizes[path_str]:
                    errors.append(f"File size changed on read-only volume: {path_str}")
                if stat.st_mtime != self.file_mtimes[path_str]:
                    errors.append(f"File mtime changed on read-only volume: {path_str}")
                with open(full_path, "rb") as f:
                    header = f.read(65536)
                    if hashlib.sha256(header).hexdigest() != self.file_hashes[path_str]:
                        errors.append(f"File content modified on read-only volume: {path_str}")

        for old_file in self.file_hashes:
            if old_file not in current_files:
                errors.append(f"File deleted on read-only volume: {old_file}")

        return len(errors) == 0, errors


def make_ocr_token(
    text: str,
    conf: float = 90.0,
    bbox: tuple[int, int, int, int] = (0, 0, 100, 20),
    page_number: int = 1,
    is_low_confidence: bool = False,
    **kwargs,
) -> OcrToken:
    """Adaptive factory creating OcrToken across contracts."""
    sig = inspect.signature(OcrToken)
    params = sig.parameters
    if "bbox" in params:
        token = OcrToken(
            text=text,
            conf=conf,
            bbox=bbox,
            page_number=page_number,
            is_low_confidence=is_low_confidence,
        )
    else:
        left, top, w, h = bbox
        token = OcrToken(
            text=text,
            conf=int(conf),
            left=left,
            top=top,
            width=w,
            height=h,
            block_num=kwargs.get("block_num", 1),
            par_num=kwargs.get("par_num", 1),
            line_num=kwargs.get("line_num", 1),
            word_num=kwargs.get("word_num", 1),
        )
        if hasattr(token, "page_number"):
            token.page_number = page_number
        if hasattr(token, "is_low_confidence"):
            token.is_low_confidence = is_low_confidence
    return token


def make_logical_line(
    tokens: list[OcrToken] | None = None,
    text: str | None = None,
    bbox: tuple[int, int, int, int] | None = None,
    page_number: int = 1,
    y_center: float | None = None,
) -> LogicalLine:
    """Adaptive factory creating LogicalLine across contracts."""
    if tokens is None and text is not None:
        bx = bbox or (0, 0, 100, 20)
        tokens = [make_ocr_token(word, 90.0, bx, page_number=page_number) for word in text.split()]
    toks = tokens or []
    sig = inspect.signature(LogicalLine)
    params = sig.parameters
    kwargs: dict[str, Any] = {"tokens": toks}
    if "y_center" in params and y_center is not None:
        kwargs["y_center"] = y_center
    if "bbox" in params and bbox is not None:
        kwargs["bbox"] = bbox
    if "text" in params and text is not None:
        kwargs["text"] = text
    if "page_number" in params:
        kwargs["page_number"] = page_number

    return LogicalLine(**kwargs)


def make_table_column(
    semantic_type: str,
    x_min: int,
    x_max: int,
    header_text: str = "",
) -> TableColumn:
    """Adaptive factory creating TableColumn across contracts."""
    sig = inspect.signature(TableColumn)
    params = sig.parameters
    if "name" in params:
        return TableColumn(name=semantic_type, x_min=x_min, x_max=x_max)  # type: ignore
    else:
        return TableColumn(
            header_text=header_text or semantic_type,
            semantic_type=semantic_type,
            x_center=(x_min + x_max) // 2,
            x_left=x_min,
            x_right=x_max,
        )
