"""Shared fixtures: isolated database guard, schema bootstrap, observations log."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

import dbtools

RESULTS = Path(__file__).resolve().parent / "results"
_OBSERVATIONS: list[dict] = []


def pytest_addoption(parser):
    parser.addoption("--run-realdata", action="store_true",
                     help="load the full raw CSVs into the isolated test database")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-realdata"):
        return
    skip = pytest.mark.skip(reason="full raw-data differential is opt-in: pass --run-realdata")
    for item in items:
        if "realdata" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_db_url():
    url = dbtools.guarded_url()
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set; database tests need the isolated test container")
    dbtools.init_schema(url)
    return url


@pytest.fixture(scope="session")
def admin_conn(test_db_url):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean_db(test_db_url, admin_conn):
    """Empty every CityLens table in the *test* database before the test."""
    dbtools.truncate_all(admin_conn)
    return admin_conn


@pytest.fixture
def tx(test_db_url):
    """A connection whose transaction is always rolled back."""
    conn = psycopg.connect(test_db_url)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def observe(request):
    """Record uncertain records and probe observations for REPORT.md."""
    def record(kind: str, payload: dict):
        _OBSERVATIONS.append({"test": request.node.nodeid, "kind": kind, **payload})
    return record


def pytest_sessionfinish(session, exitstatus):
    if not _OBSERVATIONS:
        return
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "observations.json").write_text(
        dbtools.scrub(json.dumps(_OBSERVATIONS, indent=2, default=str)), encoding="utf-8")
