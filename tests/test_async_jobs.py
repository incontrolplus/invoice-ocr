"""Tests for Pillar 5: REST API Asynchronous Task Queue & Background Job Processing."""

import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from fastapi.testclient import TestClient

from api_server import app, job_manager
from invoice_ocr import DEFAULT_OCR_CACHE_DIR
from tests.e2e.test_helpers import create_synthetic_test_image


class TestAsyncJobs(unittest.TestCase):
    """Test suite for Background Task Queue endpoints (/v1/jobs/*)."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # Clear jobs between tests
        with job_manager._lock:
            job_manager._jobs.clear()

    def test_list_jobs_empty(self):
        """Test listing jobs returns empty list when no jobs queued."""
        resp = self.client.get("/v1/jobs")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_jobs"], 0)
        self.assertEqual(data["jobs"], [])

        # Parity check on /api/v1/jobs
        resp_api = self.client.get("/api/v1/jobs")
        self.assertEqual(resp_api.status_code, 200)

    def test_get_nonexistent_job_returns_404(self):
        """Test querying an unknown job ID returns HTTP 404."""
        resp = self.client.get("/v1/jobs/non_existent_uuid_123")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"].lower())

    def test_queue_batch_upload_job_success(self):
        """Test uploading files to /v1/jobs/batch executes in background and reaches completed state."""
        img1 = create_synthetic_test_image("ФАКТУРА 0000000501 ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА 100.00")
        img2 = create_synthetic_test_image("ФАКТУРА 0000000502 ПОЛУЧАТЕЛ ООД ЕИК 114500333 СУМА 200.00")

        _, buf1 = cv2.imencode(".png", img1)
        _, buf2 = cv2.imencode(".png", img2)

        files = [
            ("files", ("inv1.png", io.BytesIO(buf1.tobytes()), "image/png")),
            ("files", ("inv2.png", io.BytesIO(buf2.tobytes()), "image/png")),
        ]

        resp = self.client.post("/v1/jobs/batch?workers=2", files=files)
        self.assertEqual(resp.status_code, 202)
        create_data = resp.json()
        self.assertIn("job_id", create_data)
        job_id = create_data["job_id"]
        self.assertEqual(create_data["status"], "queued")
        self.assertIn(job_id, create_data["check_status_url"])

        # TestClient executes background tasks, so job should now be completed
        resp_job = self.client.get(f"/v1/jobs/{job_id}")
        self.assertEqual(resp_job.status_code, 200)
        job_data = resp_job.json()

        self.assertEqual(job_data["status"], "completed")
        self.assertEqual(job_data["progress"]["total_documents"], 2)
        self.assertEqual(job_data["progress"]["processed_documents"], 2)
        self.assertEqual(job_data["progress"]["percent"], 100.0)
        self.assertIsNotNone(job_data["result"])
        self.assertEqual(job_data["result"]["summary"]["total_documents"], 2)
        self.assertEqual(job_data["result"]["summary"]["processed_successfully"], 2)

    def test_queue_batch_dir_job_success(self):
        """Test queuing a directory job via /v1/jobs/batch-dir executes in background."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            p_in = Path(tmp_in_str)
            p_out = Path(tmp_out_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000503 ДИРЕКТОРИЯ ТЕСТ")
            cv2.imwrite(str(p_in / "dir_inv.png"), img)

            payload = {
                "input_dir": str(p_in),
                "output_dir": str(p_out),
                "workers": 2,
            }
            resp = self.client.post("/v1/jobs/batch-dir", json=payload)
            self.assertEqual(resp.status_code, 202)
            create_data = resp.json()
            job_id = create_data["job_id"]

            resp_job = self.client.get(f"/v1/jobs/{job_id}")
            self.assertEqual(resp_job.status_code, 200)
            job_data = resp_job.json()
            self.assertEqual(job_data["status"], "completed")
            self.assertEqual(job_data["progress"]["percent"], 100.0)
            self.assertTrue((p_out / "dir_inv.json").exists())

    def test_queue_batch_dir_nonexistent_fails_job(self):
        """Test non-existent directory returns 404 upfront."""
        payload = {
            "input_dir": "/path/to/nonexistent/directory/xyz123",
            "output_dir": "results/",
        }
        resp = self.client.post("/v1/jobs/batch-dir", json=payload)
        self.assertEqual(resp.status_code, 404)

    def test_batch_files_endpoint_with_async_mode(self):
        """Test POST /api/v1/invoices/batch with async_mode=true returns 202 with job_id."""
        img = create_synthetic_test_image("ФАКТУРА 0000000504 АСИНХРОНЕН РЕЖИМ")
        _, buf = cv2.imencode(".png", img)
        files = [("files", ("inv.png", io.BytesIO(buf.tobytes()), "image/png"))]

        resp = self.client.post("/api/v1/invoices/batch?async_mode=true", files=files)
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("job_id", data)

        # Check job status
        job_id = data["job_id"]
        job_resp = self.client.get(f"/api/v1/jobs/{job_id}")
        self.assertEqual(job_resp.status_code, 200)
        self.assertEqual(job_resp.json()["status"], "completed")

    def test_batch_dir_endpoint_with_async_mode(self):
        """Test POST /api/v1/invoices/batch-dir with async_mode=true returns 202 with job_id."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            p_in = Path(tmp_in_str)
            p_out = Path(tmp_out_str)
            img = create_synthetic_test_image("ФАКТУРА 0000000505 ДИР АСИНХРОНЕН")
            cv2.imwrite(str(p_in / "inv.png"), img)

            payload = {
                "input_dir": str(p_in),
                "output_dir": str(p_out),
                "async_mode": True,
            }
            resp = self.client.post("/api/v1/invoices/batch-dir", json=payload)
            self.assertEqual(resp.status_code, 202)
            data = resp.json()
            self.assertIn("job_id", data)

            job_id = data["job_id"]
            job_resp = self.client.get(f"/v1/jobs/{job_id}")
            self.assertEqual(job_resp.status_code, 200)
            self.assertEqual(job_resp.json()["status"], "completed")

    def test_delete_job(self):
        """Test deleting a job record removes it from store."""
        img = create_synthetic_test_image("ФАКТУРА 0000000506 ТРИЕНЕ")
        _, buf = cv2.imencode(".png", img)
        files = [("files", ("inv.png", io.BytesIO(buf.tobytes()), "image/png"))]

        resp = self.client.post("/v1/jobs/batch", files=files)
        job_id = resp.json()["job_id"]

        del_resp = self.client.delete(f"/v1/jobs/{job_id}")
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(del_resp.json()["status"], "ok")

        # Confirm 404 after deletion
        get_resp = self.client.get(f"/v1/jobs/{job_id}")
        self.assertEqual(get_resp.status_code, 404)

    def test_orphaned_job_reconciliation_on_startup(self):
        """Test that uncompleted jobs left in processing or queued state are reconciled to interrupted on startup."""
        from database import get_db_session, PersistentJobRecord
        from api_server import JobManager, JobStatus

        orphan_job_id = "orphan_proc_test_999"
        queued_job_id = "orphan_queued_test_998"

        with get_db_session() as db:
            # Clean up if existed
            db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id.in_([orphan_job_id, queued_job_id])).delete()
            db.commit()

            rec1 = PersistentJobRecord(
                job_id=orphan_job_id,
                status=JobStatus.PROCESSING.value,
                request_type="batch-dir",
                created_at=time.time() - 100,
                started_at=time.time() - 90,
            )
            rec2 = PersistentJobRecord(
                job_id=queued_job_id,
                status=JobStatus.QUEUED.value,
                request_type="batch-dir",
                created_at=time.time() - 50,
            )
            db.add_all([rec1, rec2])
            db.commit()

        # Instantiate fresh manager to trigger _load_from_db reconciliation
        fresh_mgr = JobManager(max_history=100)
        job1 = fresh_mgr.get_job(orphan_job_id)
        job2 = fresh_mgr.get_job(queued_job_id)

        self.assertIsNotNone(job1)
        self.assertEqual(job1["status"], JobStatus.INTERRUPTED.value)
        self.assertIn("interrupted", job1["error"].lower())

        self.assertIsNotNone(job2)
        self.assertEqual(job2["status"], JobStatus.INTERRUPTED.value)

        # Cleanup
        with get_db_session() as db:
            db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id.in_([orphan_job_id, queued_job_id])).delete()
            db.commit()

    def test_retry_interrupted_or_failed_job(self):
        """Test POST /v1/jobs/{job_id}/retry resets interrupted or failed job to queued."""
        from database import get_db_session, PersistentJobRecord
        from api_server import JobStatus

        retry_job_id = "job_to_retry_111"
        with get_db_session() as db:
            db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id == retry_job_id).delete()
            db.commit()
            rec = PersistentJobRecord(
                job_id=retry_job_id,
                status=JobStatus.INTERRUPTED.value,
                request_type="batch-dir",
                created_at=time.time() - 60,
                error_message="Interrupted by reboot",
                metadata_json=json.dumps({"input_dir": "/tmp"}),
            )
            db.add(rec)
            db.commit()

        # Query job manager to load it
        job_manager.get_job(retry_job_id)

        retry_resp = self.client.post(f"/v1/jobs/{retry_job_id}/retry")
        self.assertEqual(retry_resp.status_code, 202)
        data = retry_resp.json()
        self.assertEqual(data["status"], "queued")
        self.assertIn("re-queued", data["message"])

        # Check job status in manager
        j = job_manager.get_job(retry_job_id)
        self.assertIn(j["status"], [JobStatus.QUEUED.value, JobStatus.PROCESSING.value, JobStatus.COMPLETED.value, JobStatus.FAILED.value])
        self.assertIsNone(j["error"])

        # Clean up
        with get_db_session() as db:
            db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id == retry_job_id).delete()
            db.commit()


if __name__ == "__main__":
    unittest.main()
