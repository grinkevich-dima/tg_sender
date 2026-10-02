import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import auth, db, worker
from .config import ALLOWED_HOSTS, BASE_DIR, SECRET_KEY, SECURE_COOKIES
from .tg import tgm
from .web import accounts, ai_routes, campaigns, chat_search, inbox, leads, misc, segments, templates_routes, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    app.state.leader = db.acquire_leader()
    task = None
    if app.state.leader:
        await tgm.start_all()
        task = asyncio.create_task(worker.run())
    else:
        db.log("Уже работает другая копия панели: в этой Telegram и отправка не запущены (защита сессий)", "error")
    yield
    if task:
        task.cancel()
        await worker.stop()
        await tgm.stop_all()
    db.release_leader()
    db.close()


app = FastAPI(lifespan=lifespan, title="TG Sender")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
for r in (users.router, misc.router, accounts.router, leads.router, segments.router, templates_routes.router,
          campaigns.router, chat_search.router, inbox.router, ai_routes.router):
    app.include_router(r)

# порядок: последний добавленный — внешний. Хост → cookie-сессия → вход и защита форм
app.middleware("http")(auth.auth_middleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="tgs_session", max_age=14 * 24 * 3600,
                   same_site="lax", https_only=SECURE_COOKIES)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
