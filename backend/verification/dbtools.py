"""Isolated test-database helpers and mismatch reporting.

Production code is exercised only through its documented interfaces: the
`db.py init|load|count` and `collision_ingest.py` command lines, the SQL
functions installed by `db.py init`, and the documented `collision_queries`
Python wrappers. Nothing here reads or imports production implementation code
except those wrappers, which are called as black boxes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.conninfo import conninfo_to_dict

from refimpl.result import Expected

BACKEND = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DEV_PORT = "54329"
GID_TABLES = ["service_requests", "ingestions"]
COLLISION_TABLES = ["traffic_collisions", "collision_source_rows", "collision_imports"]
TABLES = COLLISION_TABLES + GID_TABLES


class UnsafeDatabase(RuntimeError):
    pass


def guarded_url() -> str | None:
    """Return TEST_DATABASE_URL after safety checks, or None if unset."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return None
    info = conninfo_to_dict(url)
    if not str(info.get("dbname", "")).endswith("_test"):
        raise UnsafeDatabase("TEST_DATABASE_URL database name must end with '_test'")
    if str(info.get("port", "5432")) == DEV_PORT:
        raise UnsafeDatabase(f"TEST_DATABASE_URL must not use the development port {DEV_PORT}")
    dev = os.environ.get("DATABASE_URL")
    if dev:
        d = conninfo_to_dict(dev)
        if (d.get("host"), d.get("port"), d.get("dbname")) == (info.get("host"), info.get("port"), info.get("dbname")):
            raise UnsafeDatabase("TEST_DATABASE_URL points at DATABASE_URL")
    return url


def mask(text: str, url: str) -> str:
    password = conninfo_to_dict(url).get("password")
    return text.replace(password, "***") if password else text


def run_cli(url: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a production command line with DATABASE_URL pointed at the test DB."""
    env = {**os.environ, "DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([PYTHON, *args], cwd=BACKEND.parent, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    proc.stdout, proc.stderr = mask(proc.stdout, url), mask(proc.stderr, url)
    return proc


def init_schema(url: str) -> None:
    proc = run_cli(url, str(BACKEND / "db.py"), "init")
    if proc.returncode:
        raise RuntimeError(f"db.py init failed:\n{proc.stderr[-2000:]}")


def load_gid(url: str, csv_path) -> subprocess.CompletedProcess:
    return run_cli(url, str(BACKEND / "db.py"), "load", str(csv_path))


def load_collisions(url: str, csv_path, *extra: str) -> subprocess.CompletedProcess:
    return run_cli(url, str(BACKEND / "collision_ingest.py"), str(csv_path), *extra)


def truncate_all(conn: psycopg.Connection, tables=TABLES) -> None:
    conn.execute("TRUNCATE " + ", ".join(f"citylens.{t}" for t in tables) + " RESTART IDENTITY CASCADE")


def set_locations(conn, points: dict[str, tuple[float, float]]) -> None:
    """Mark synthetic rows as geocoded, satisfying the table's CHECK constraints."""
    for rid, (lat, lng) in points.items():
        conn.execute(
            """UPDATE citylens.traffic_collisions
               SET location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                   geocode_status = 'matched', geocode_precision = 'block_approximate',
                   geocode_provider = 'verification-synthetic', geocoded_at = now(),
                   geocode_details = '{"synthetic": true}'::jsonb
               WHERE report_id = %s""", (lng, lat, rid))


def ts(value) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value, tz="UTC")


def snapshot(conn) -> dict:
    """Comparable state of every verification-relevant table."""
    out = {}
    for table, key in [("service_requests", "service_request_id"), ("traffic_collisions", "report_id"),
                       ("ingestions", "id"), ("collision_imports", "id"), ("collision_source_rows", "import_id, row_number")]:
        rows = conn.execute(f"SELECT to_jsonb(t)::text FROM citylens.{table} t ORDER BY {key}").fetchall()
        out[table] = [r[0] for r in rows]
    return out


# ---------------------------------------------------------------- reporting
def raw_rows(frame: pd.DataFrame, id_column: str, ids) -> list[dict]:
    """Exact input CSV rows (all occurrences) for the given IDs."""
    ids = set(ids)
    sub = frame[frame[id_column].str.strip().isin(ids)]
    return sub.to_dict(orient="records")


def diff_ids(label: str, expected: Expected, observed, frame: pd.DataFrame | None = None,
             id_column: str | None = None, params: dict | None = None) -> tuple[str | None, dict]:
    """Compare an observed ID collection with the reference.

    Returns (failure message or None, uncertainty record). Uncertain IDs are
    never failures; they are returned for separate reporting.
    """
    observed = [str(x) for x in observed]
    obs = set(observed)
    allowed = expected.ids | set(expected.uncertain)
    missing = sorted(expected.ids - obs)
    unexpected = sorted(obs - allowed)
    duplicates = sorted({x for x in observed if observed.count(x) > 1})
    uncertain = {rid: {"reason": why, "observed": rid in obs} for rid, why in expected.uncertain.items()
                 if not rid.startswith("__")}
    message = None
    if missing or unexpected or duplicates:
        lines = [f"MISMATCH {label}", f"  parameters: {json.dumps(params or {}, default=str)}",
                 f"  expected ids ({len(expected.ids)}): {sorted(expected.ids)}",
                 f"  uncertain ids: {expected.uncertain}",
                 f"  observed ids ({len(observed)}): {sorted(obs)}",
                 f"  missing (expected, not observed): {missing}",
                 f"  unexpected (observed, not expected): {unexpected}"]
        if duplicates:
            lines.append(f"  duplicated in observed result: {duplicates}")
        if frame is not None and id_column:
            for row in raw_rows(frame, id_column, missing + unexpected)[:40]:
                lines.append(f"  input row: {row}")
        message = "\n".join(lines)
    return message, {"label": label, "params": params, "uncertain": uncertain}


def count_in_range(label: str, expected: Expected, observed: int, params: dict) -> str | None:
    low, high = expected.count_range
    if low <= observed <= high:
        return None
    return (f"MISMATCH {label}\n  parameters: {json.dumps(params, default=str)}\n"
            f"  expected count: {low}" + (f"..{high} (uncertain: {expected.uncertain})" if high != low else "") +
            f"\n  observed count: {observed}\n  expected ids: {sorted(expected.ids)}")


SECRET_RE = re.compile(r"postgres(?:ql)?://[^\s]+")


def scrub(text: str) -> str:
    return SECRET_RE.sub("postgresql://***", text)
