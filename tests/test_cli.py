# tests/test_cli.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from hub.cli import cli_loop


def test_cli_module_imports():
    """Verify hub.cli module is importable after cleanup."""
    import hub.cli
    assert hasattr(hub.cli, "cli_loop")
    assert hasattr(hub.cli, "main")
