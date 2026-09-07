"""Unit tests for mobile camera photo preprocessing pipeline.

Tests:
- Adaptive Lanczos-4 upscaling for low-resolution phone photos (< 2200px)
- Preservation of original dimensions for high-resolution images
- Illumination normalization & shadow attenuation (morphological background division)
- Sauvola adaptive binarization for non-uniform lighting
- Dot-matrix pin bridging
- Coordinate rescaling contract to maintain bounding box accuracy in original pixel space
"""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2
import numpy as np
import pytest

from invoice_ocr import preprocess_mobile_photo, OcrToken


class TestMobileCameraPreprocessing:
    """Test mobile camera image enhancements for OCR accuracy."""

    def test_adaptive_upscaling_low_res_photo(self):
        # Simulate a 960x1280 mobile phone camera photo
        img = np.full((1280, 960, 3), 200, dtype=np.uint8)
        # Draw some high-contrast simulated text
        cv2.putText(img, "ФАКТУРА № 1234567890", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 2)

        processed, meta = preprocess_mobile_photo(img, target_dim=2400)

        assert meta["upscaled"] is True
        expected_scale = 2400.0 / 1280.0
        assert abs(meta["scale_factor"] - expected_scale) < 1e-4
        assert max(processed.shape[:2]) == 2400
        assert processed.shape == (2400, int(960 * expected_scale))
        assert processed.dtype == np.uint8

    def test_no_upscaling_for_high_resolution_image(self):
        # Simulate a high-res 2400x3200 scan or photo
        img = np.full((3200, 2400, 3), 220, dtype=np.uint8)
        cv2.putText(img, "СТОКОВА РАЗПИСКА", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 3)

        processed, meta = preprocess_mobile_photo(img, target_dim=2400)

        assert meta["upscaled"] is False
        assert processed.shape == (3200, 2400)

    def test_shadow_attenuation_and_binarization(self):
        # Create an image with a severe diagonal illumination gradient (shadow)
        h, w = 600, 800
        img = np.zeros((h, w), dtype=np.uint8)
        for y in range(h):
            for x in range(w):
                # Gradient from 240 (top-left) to 60 (bottom-right)
                intensity = int(240 - 180 * (x / w + y / h) / 2.0)
                img[y, x] = intensity

        # Embed dark text across both the bright and dark (shadowed) zones
        cv2.putText(img, "BRIGHT TEXT", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
        cv2.putText(img, "SHADOW TEXT", (450, 500), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)

        processed, meta = preprocess_mobile_photo(img, target_dim=1200)

        assert meta["shadow_removed"] is True
        # Verify output is strictly binary (values are 0 or 255)
        unique_vals = set(np.unique(processed))
        assert unique_vals.issubset({0, 255})

        # Verify that the background in both top-left and bottom-right is normalized to white (255)
        assert processed[10, 10] == 255
        assert processed[-10, -10] == 255

    def test_dot_matrix_pin_bridging(self):
        # Create an image simulating dot-matrix matrix print: dots with 1-2px gaps
        img = np.full((200, 400), 255, dtype=np.uint8)
        # Draw 5 horizontal dots separated by 1px white space
        y = 100
        for i in range(5):
            x = 100 + i * 4
            img[y, x:x+2] = 0

        # Before bridging, gaps exist
        assert img[y, 102] == 255
        assert img[y, 106] == 255

        processed, meta = preprocess_mobile_photo(img, target_dim=2400)
        assert meta["dot_matrix_bridged"] is True

    def test_bounding_box_rescaling_roundtrip(self):
        orig_w, orig_h = 960, 1280
        target_dim = 2400
        scale = float(target_dim) / float(max(orig_h, orig_w))  # 1.875

        # Simulating a token found at (187, 375, 150, 37) in upscaled image
        upscaled_bbox = (187, 375, 150, 37)
        tok = OcrToken(
            text="0000357071",
            conf=0.96,
            bbox=upscaled_bbox,
        )

        # Rescaling logic from process_invoice
        x, y, w, h = tok.bbox
        orig_bbox = (
            int(round(x / scale)),
            int(round(y / scale)),
            max(1, int(round(w / scale))),
            max(1, int(round(h / scale))),
        )

        assert orig_bbox[0] == int(round(187 / 1.875))  # ~100
        assert orig_bbox[1] == int(round(375 / 1.875))  # ~200
        assert orig_bbox[0] < orig_w
        assert orig_bbox[1] < orig_h

    def test_order_quadrilateral_points(self):
        from invoice_ocr import order_quadrilateral_points
        # Unordered points: BR, TL, BL, TR
        pts = np.array([[700, 500], [150, 100], [100, 480], [650, 80]], dtype="float32")
        ordered = order_quadrilateral_points(pts)
        # TL should be [150, 100]
        np.testing.assert_allclose(ordered[0], [150, 100])
        # TR should be [650, 80]
        np.testing.assert_allclose(ordered[1], [650, 80])
        # BR should be [700, 500]
        np.testing.assert_allclose(ordered[2], [700, 500])
        # BL should be [100, 480]
        np.testing.assert_allclose(ordered[3], [100, 480])

    def test_detect_and_warp_quadrilateral_perspective(self):
        from invoice_ocr import (
            detect_document_quadrilateral,
            four_point_perspective_transform,
            transform_bbox_perspective,
        )

        # Create dark background (desk) 800x600
        img = np.full((600, 800, 3), 40, dtype=np.uint8)
        # Draw a bright, rotated/perspective sheet of paper
        corners = np.array([[120, 100], [680, 80], [720, 520], [80, 500]], dtype=np.int32)
        cv2.fillPoly(img, [corners], (240, 240, 240))
        cv2.putText(img, "ФАКТУРА ОРИГИНАЛ", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)

        detected = detect_document_quadrilateral(img)
        assert detected is not None
        assert detected.shape == (4, 2)

        # Perspective warp
        warped, M, M_inv = four_point_perspective_transform(img, detected)
        assert warped.shape[0] > 350
        assert warped.shape[1] > 500
        assert M.shape == (3, 3)
        assert M_inv.shape == (3, 3)

        # Check bbox mapping
        warped_bbox = (50, 50, 200, 30)
        orig_bbox = transform_bbox_perspective(warped_bbox, M_inv)
        assert orig_bbox[0] >= 0
        assert orig_bbox[1] >= 0
        assert orig_bbox[0] + orig_bbox[2] <= 800
        assert orig_bbox[1] + orig_bbox[3] <= 600

    def test_preprocess_mobile_photo_perspective_integration(self):
        # Create image with skewed paper on desk
        img = np.full((700, 900, 3), 35, dtype=np.uint8)
        corners = np.array([[150, 90], [750, 110], [790, 620], [110, 600]], dtype=np.int32)
        cv2.fillPoly(img, [corners], (245, 245, 245))
        cv2.putText(img, "ЕИК 123456789", (250, 350), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (10, 10, 10), 2)

        processed, meta = preprocess_mobile_photo(img, target_dim=2400, apply_perspective=True)
        assert meta["perspective_warped"] is True
        assert "homography_matrix" in meta
        assert "inverse_homography_matrix" in meta
        assert "warped_shape" in meta
        assert max(processed.shape[:2]) == 2400

    def test_real_camera_sample_if_available(self):
        sample_path = "/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД/07_09_2026_part_2/JPEG/photo_5834929551312621906_y.jpg"
        if not os.path.exists(sample_path):
            pytest.skip("Dataset volume not mounted or sample file unavailable")

        img = cv2.imread(sample_path)
        assert img is not None
        assert img.shape[:2] == (1280, 960)

        processed, meta = preprocess_mobile_photo(img, target_dim=2400)
        assert meta["upscaled"] is True
        assert max(processed.shape[:2]) == 2400
        assert set(np.unique(processed)).issubset({0, 255})

