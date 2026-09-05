"""Pytest fixtures and configuration for E2E tests."""
import sys
from pathlib import Path
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.e2e.test_helpers import DirectoryStateSnapshot, ACCEPTANCE_DIR


@pytest.fixture(scope="session")
def external_volume_snapshot():
    """Takes a snapshot of the external acceptance dataset before and after tests."""
    if ACCEPTANCE_DIR.exists():
        snapshot = DirectoryStateSnapshot(ACCEPTANCE_DIR)
        yield snapshot
        is_clean, errors = snapshot.verify_unchanged()
        assert is_clean, f"External volume was modified during tests: {errors}"
    else:
        yield None
