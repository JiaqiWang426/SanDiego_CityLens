"""Run the local API with a psycopg-compatible event loop on Windows."""
import argparse
import asyncio
import sys

import uvicorn


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config('backend.main:app', host=args.host, port=args.port))
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == 'win32' else None) as runner:
        runner.run(server.serve())
