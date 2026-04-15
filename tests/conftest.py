import pytest
import tempfile
import os


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_tasks.db")
