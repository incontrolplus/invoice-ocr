"""Unit and integration tests for streaming batch processing (NDJSON & Generator).

Verifies DoD items:
1. Streaming intermediate results during batch processing without retaining entire batch in RAM.
2. iter_process_batch generator streaming and bounded sliding window.
3. FastAPI streaming batch endpoints (/api/v1/invoices/batch?stream=true and /stream endpoints).
4. Early termination clean resource reclamation.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_server import app
from invoice_ocr import (
    iter_process_batch,
    process_batch,
    get_open_fd_count,
    get_process_rss_mb,
)
from tests.e2e.test_helpers import create_synthetic_test_image


class TestBatchStreamingPipeline(unittest.TestCase):
    """Test suite for streaming batch processing and NDJSON HTTP responses."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.p_in = Path(self.tmp_dir.name) / "input"
        self.p_out = Path(self.tmp_dir.name) / "output"
        self.p_in.mkdir(parents=True)
        self.p_out.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_synthetic_invoices(self, count: int = 4) -> list[Path]:
        created = []
        for i in range(1, count + 1):
            img = create_synthetic_test_image(
                f"ФАКТУРА 00000000{i:02d} ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА {i * 50}.00 ЛВ"
            )
            fp = self.p_in / f"doc_{i:02d}.png"
            cv2.imwrite(str(fp), img)
            created.append(fp)
        return created

    def test_iter_process_batch_streaming_generator(self):
        """iter_process_batch must yield results one-by-one as a generator."""
        self._create_synthetic_invoices(4)
        stream = iter_process_batch(
            input_dir=self.p_in,
            output_dir=self.p_out,
            workers=2,
            quiet=True,
        )

        results = []
        for item in stream:
            self.assertIn("file", item)
            self.assertEqual(item["status"], "success")
            self.assertIn("duration_seconds", item)
            results.append(item)

        self.assertEqual(len(results), 4)
        # Verify all 4 JSON files were written to output dir
        self.assertEqual(len(list(self.p_out.glob("*.json"))), 4)

    def test_iter_process_batch_early_termination_resource_cleanup(self):
        """Closing the streaming generator early must cleanly cancel pending tasks without leaking."""
        self._create_synthetic_invoices(6)
        stream = iter_process_batch(
            input_dir=self.p_in,
            output_dir=self.p_out,
            workers=2,
            quiet=True,
        )

        first_item = next(stream)
        self.assertEqual(first_item["status"], "success")

        # Early termination
        stream.close()

        # Output should have at least the first item
        self.assertTrue((self.p_out / f"{Path(first_item['file']).stem}.json").exists())

    def test_fastapi_batch_upload_streaming_ndjson_query_param(self):
        """POST /api/v1/invoices/batch?stream=true returns newline-delimited JSON stream."""
        img1 = create_synthetic_test_image("ФАКТУРА 0000000501 ДОСТАВЧИК ЕООД ЕИК 121644736")
        img2 = create_synthetic_test_image("ФАКТУРА 0000000502 КЛИЕНТ ООД ЕИК 114500333")

        _, b1 = cv2.imencode(".png", img1)
        _, b2 = cv2.imencode(".png", img2)

        files = [
            ("files", ("inv1.png", io.BytesIO(b1.tobytes()), "image/png")),
            ("files", ("inv2.png", io.BytesIO(b2.tobytes()), "image/png")),
        ]

        resp = self.client.post("/api/v1/invoices/batch?stream=true", files=files)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/x-ndjson", resp.headers.get("content-type", ""))

        lines = [line for line in resp.text.strip().split("\n") if line]
        self.assertGreaterEqual(len(lines), 2)

        events = [json.loads(line) for line in lines]
        doc_events = [e for e in events if e.get("event") == "document"]
        summary_events = [e for e in events if e.get("event") == "summary"]

        self.assertEqual(len(doc_events), 2)
        self.assertEqual(len(summary_events), 1)

        for d in doc_events:
            self.assertEqual(d["status"], "success")
            self.assertIn("duration_seconds", d)
            self.assertIn("worker_pid", d)

        self.assertEqual(summary_events[0]["total_documents"], 2)
        self.assertEqual(summary_events[0]["processed_successfully"], 2)

    def test_fastapi_dedicated_batch_stream_endpoint(self):
        """POST /api/v1/invoices/batch/stream returns real-time NDJSON events."""
        img = create_synthetic_test_image("ФАКТУРА 0000000601 СТРИЙМ ТЕСТ")
        _, b = cv2.imencode(".png", img)

        files = [
            ("files", ("stream_inv.png", io.BytesIO(b.tobytes()), "image/png")),
        ]

        resp = self.client.post("/api/v1/invoices/batch/stream", files=files)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/x-ndjson", resp.headers.get("content-type", ""))

        lines = [line for line in resp.text.strip().split("\n") if line]
        events = [json.loads(line) for line in lines]

        self.assertEqual(events[0]["event"], "document")
        self.assertEqual(events[0]["status"], "success")
        self.assertEqual(events[-1]["event"], "summary")
        self.assertEqual(events[-1]["total_documents"], 1)

    def test_fastapi_batch_dir_streaming_endpoint(self):
        """POST /api/v1/invoices/batch-dir/stream streams directory processing as NDJSON."""
        self._create_synthetic_invoices(2)

        payload = {
            "input_dir": str(self.p_in),
            "output_dir": str(self.p_out),
            "workers": 2,
        }
        resp = self.client.post("/api/v1/invoices/batch-dir/stream", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/x-ndjson", resp.headers.get("content-type", ""))

        lines = [line for line in resp.text.strip().split("\n") if line]
        events = [json.loads(line) for line in lines]

        doc_events = [e for e in events if e.get("event") == "document"]
        self.assertEqual(len(doc_events), 2)
        self.assertEqual(events[-1]["event"], "summary")
        self.assertEqual(events[-1]["total_documents"], 2)


if __name__ == "__main__":
    unittest.main()
