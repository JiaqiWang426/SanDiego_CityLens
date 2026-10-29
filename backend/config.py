"""Shared environment bootstrap for the API and explicit CLI entry points."""
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def load_environment() -> bool:
    """Load optional root .env values, overriding existing process values.

    No credentials are returned or logged. Import alone does not read the file.
    """
    return load_dotenv(dotenv_path=ENV_FILE,override=True,verbose=False,encoding='utf-8-sig')
