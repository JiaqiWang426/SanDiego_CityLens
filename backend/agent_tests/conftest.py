"""Keep dotenv loading isolated from developer secrets during tests."""
import pytest
import os
from unittest.mock import patch

from backend import config


@pytest.fixture(autouse=True)
def isolated_dotenv_file(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ENV_FILE',tmp_path/'test-config'/'.env')
    # load_dotenv mutates os.environ directly, outside monkeypatch's tracking.
    with patch.dict(os.environ):
        yield
