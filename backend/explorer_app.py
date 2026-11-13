"""Course demo: python -m uvicorn backend.explorer_app:app --port 8000."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .explorer_data import ROOT, ensure_cache
from .explorer_api import router


@asynccontextmanager
async def lifespan(app):
    ensure_cache()
    yield


app = FastAPI(title='District 1 Service Request Explorer', lifespan=lifespan)
app.include_router(router)
app.mount('/assets', StaticFiles(directory=ROOT / 'frontend'), name='assets')


@app.get('/', include_in_schema=False)
def home():
    return FileResponse(ROOT / 'frontend/explorer.html')
