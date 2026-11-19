from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text

from app.config import Settings
from app.database import make_database
from app.demo import DemoClient
from app.errors import AppError
from app.keitaro import KeitaroClient
from app.schemas import CreateCampaign, DraftAction, FetchRequest, Revision
from app.services import CampaignService

HERE = Path(__file__).resolve().parent


def create_app(settings=None, *, client=None):
    settings = settings or Settings()
    engine, sessions = make_database(settings.database_url)
    upstream = client or (DemoClient(sessions) if settings.app_mode == "demo" else KeitaroClient(settings))
    service = CampaignService(settings, sessions, upstream)

    @asynccontextmanager
    async def lifespan(app):
        try:
            required = {"campaign_states", "operations", "demo_campaigns", "alembic_version"}
            if not required.issubset(set(inspect(engine).get_table_names())):
                raise RuntimeError("Database is not migrated. Run: python -m alembic upgrade head")
            yield
        finally:
            await upstream.close()
            engine.dispose()

    app = FastAPI(title="Adrobot — Keitaro wrapper", version="1.0.0", lifespan=lifespan)
    app.state.service, app.state.sessions, app.state.engine = service, sessions, engine
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": error.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo the request payload or arbitrary upstream response bodies.
        messages = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in error.errors()]
        return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/healthz")
    async def health():
        with sessions() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.app_mode}

    @app.get("/api/settings")
    async def public_settings():
        return {
            "mode": settings.app_mode,
            "campaign_prefix": settings.campaign_prefix,
            "google_url": "https://www.google.com/",
        }

    @app.get("/api/catalog")
    async def catalog():
        return await service.catalog()

    @app.get("/api/campaigns")
    async def campaigns(search: str = Query(default="", max_length=200)):
        return await service.campaigns(search)

    @app.post("/api/campaigns", status_code=201)
    async def create_campaign(body: CreateCampaign):
        return await service.create(body)

    @app.get("/api/campaigns/{campaign_id}")
    async def campaign(campaign_id: int):
        return await service.get(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/fetch")
    async def fetch(campaign_id: int, body: FetchRequest):
        return await service.fetch(campaign_id, body.revision, body.discard_draft)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/draft")
    async def draft(campaign_id: int, stream_id: int, body: DraftAction):
        return await service.change(campaign_id, stream_id, body)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/cancel")
    async def cancel(campaign_id: int, stream_id: int, body: Revision):
        return await service.cancel(campaign_id, stream_id, body.revision)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/publish")
    async def publish(campaign_id: int, stream_id: int, body: Revision):
        return await service.publish(campaign_id, stream_id, body.revision)

    return app


app = create_app()
