from fastapi import FastAPI
from sqlalchemy import text

from analytics.db import analytics_engine
from analytics.executive_dashboard import router as executive_router
from analytics.network_reports import router as network_router
from analytics.reports import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Verigence Analytics",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    app.include_router(network_router)
    app.include_router(executive_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        with analytics_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT 1 FROM analytics.dump_runs LIMIT 1"))
        return {"status": "ready"}

    return app


app = create_app()
