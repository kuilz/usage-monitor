import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .config import settings
from .database import init_db, close_db
from .proxy import router as proxy_router
from .dashboard import router as dashboard_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await init_db(settings.db_path)
    app.state.db = db
    yield
    await close_db(db)


app = FastAPI(title="Usage Monitor", lifespan=lifespan)
app.include_router(proxy_router)
app.include_router(dashboard_router)


def main():
    uvicorn.run(
        "usage_monitor.main:app",
        host=settings.proxy_host,
        port=settings.proxy_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
