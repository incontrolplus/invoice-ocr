#!/usr/bin/env python3
"""Investigate image ingestion methods (.png, .jpg, .jpeg) and path safety."""
from pathlib import Path
import cv2
import numpy as np

def load_image_cv2_direct(path: Path) -> np.ndarray:
    """Standard cv2.imread."""
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to read image from {path}")
    return img

def load_image_bytes_decode(path: Path) -> np.ndarray:
    """Robust image loader using Path.read_bytes + cv2.imdecode.
    
    Safe against non-ASCII paths (Cyrillic characters), symlinks, and permission issues.
    """
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    data = path.read_bytes()
    if len(data) == 0:
        raise ValueError(f"Empty image file: {path}")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {path}")
    return img

def test_image_loaders():
    tmp_dir = Path("/tmp/test_img_ingestion")
    tmp_dir.mkdir(exist_ok=True)
    
    # 1. Create test images in English and Cyrillic directories
    cyrillic_dir = tmp_dir / "02_КАПИНА_ФАКТУРИ_ТЕСТ"
    cyrillic_dir.mkdir(exist_ok=True)
    
    sample_img = np.zeros((100, 200, 3), dtype=np.uint8)
    sample_img[:, :] = (120, 150, 200) # BGR
    
    png_path = cyrillic_dir / "фактура_образец.png"
    jpg_path = cyrillic_dir / "фактура_образец.jpg"
    
    cv2.imwrite(str(png_path), sample_img)
    cv2.imwrite(str(jpg_path), sample_img)
    
    print(f"Created: {png_path} ({png_path.stat().st_size} bytes)")
    print(f"Created: {jpg_path} ({jpg_path.stat().st_size} bytes)")
    
    # Test loading via direct imread
    try:
        img1 = load_image_cv2_direct(png_path)
        print(f"Direct imread on Cyrillic path: OK, shape={img1.shape}")
    except Exception as e:
        print(f"Direct imread on Cyrillic path FAILED: {e}")
        
    # Test loading via bytes decode
    try:
        img2 = load_image_bytes_decode(png_path)
        print(f"Bytes decode on Cyrillic path: OK, shape={img2.shape}")
        assert np.array_equal(sample_img, img2)
    except Exception as e:
        print(f"Bytes decode on Cyrillic path FAILED: {e}")
        
    # Test corrupted image
    corrupt_img = cyrillic_dir / "повредена.jpg"
    corrupt_img.write_bytes(b"NOT_A_JPEG_FILE_HEADER_CORRUPTED")
    try:
        load_image_bytes_decode(corrupt_img)
        print("Corrupted image should have failed!")
    except Exception as e:
        print(f"Corrupted image raised expected exception: {type(e).__name__}: {e}")

    # Test empty image
    empty_img = cyrillic_dir / "празна.png"
    empty_img.write_bytes(b"")
    try:
        load_image_bytes_decode(empty_img)
        print("Empty image should have failed!")
    except Exception as e:
        print(f"Empty image raised expected exception: {type(e).__name__}: {e}")

if __name__ == "__main__":
    test_image_loaders()
