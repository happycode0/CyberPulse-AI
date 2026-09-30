"""Unit test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def fixture(tmp_path):
    """Return a fixture file path, creating it from a template if called with a filename."""
    
    def _fixture(name: str) -> Path:
        fixtures_dir = Path(__file__).parent / "fixtures"
        path = fixtures_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        return path
    
    return _fixture
