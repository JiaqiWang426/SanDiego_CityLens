"""Explicit session schema + official checkpoint migrations; no dataset writes."""
import asyncio
import os
import sys
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row

from backend.config import load_environment
from backend.agent.sessions import CHECKPOINT_OPTIONS


async def setup(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True, prepare_threshold=0,
            row_factory=dict_row, options=CHECKPOINT_OPTIONS, connect_timeout=3) as conn:
        await conn.execute((Path(__file__).parents[1]/'sql/session_history.sql').read_text(encoding='utf-8'), prepare=False)
        await AsyncPostgresSaver(conn).setup()


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    load_environment()
    url = os.getenv('SESSION_SETUP_DATABASE_URL') or os.getenv('SESSION_DATABASE_URL')
    if not url:
        raise SystemExit('SESSION_DATABASE_URL is required; there is no administrator/reader fallback.')
    try:
        asyncio.run(setup(url))
    except Exception:
        raise SystemExit('Session setup failed. Check database access and DDL grants; credentials are not logged.') from None
    print('Session schema and LangGraph checkpoint migrations are up to date.')
