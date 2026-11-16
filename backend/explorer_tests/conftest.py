"""Tests never load workstation credentials or contact a live model."""
import os
from unittest.mock import patch
import pytest
from backend import config


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENV_FILE', tmp_path / 'test.env')
    with patch.dict(os.environ):
        for name in ['AGENT_DATABASE_URL', 'SESSION_DATABASE_URL', 'OPENAI_API_KEY', 'OPENAI_MODEL']:
            os.environ.pop(name, None)
        yield
