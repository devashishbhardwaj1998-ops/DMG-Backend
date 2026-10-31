import re
from contextlib import asynccontextmanager
from pathlib import Path
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware
from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .models import User
from .services import HANDLERS, Err

def seed_admins(db, emails):
    """Create each listed email as an Admin (global access to every region) if it doesn't exist yet. Existing people are left unchanged."""
    for email in emails:
        if db.scalar(select(User).where(User.email == email)): continue
        name = " ".join(p.capitalize() for p in re.split(r"[._-]+", email.split("@")[0]) if p) or "Admin"
        db.add(User(name=name, email=email, title="Senior Associate", is_admin=True, is_reviewer=True))
    db.commit()

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_admins(db, settings.admin_emails)
    yield

app = FastAPI(title="DMG Assignment Hub", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.https_only, same_site="lax", max_age=60 * 60 * 12)
oauth = OAuth()
if settings.google_client_id:
    oauth.register("google", client_id=settings.google_client_id, client_secret=settings.google_client_secret,
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration", client_kwargs={"scope": "openid email profile"})

def current_user(request: Request, db=Depends(get_db)):
    email = request.session.get("email")
    u = db.scalar(select(User).where(User.email == email, User.active)) if email else None
    if not u: raise Err("Not signed in", 401)
    return u

@app.exception_handler(Err)
async def err_handler(request, e): return JSONResponse({"error": e.msg}, status_code=e.code)

@app.get("/healthz")
def health(): return {"ok": True}

@app.get("/auth/login")
async def login(request: Request):
    if not settings.google_client_id: return HTMLResponse("Google sign-in is not configured (set GOOGLE_CLIENT_ID/SECRET).", 500)
    return await oauth.google.authorize_redirect(request, str(request.url_for("callback")))

@app.get("/auth/callback", name="callback")
async def callback(request: Request, db=Depends(get_db)):
    tok = await oauth.google.authorize_access_token(request); info = tok.get("userinfo") or {}
    email = str(info.get("email", "")).lower()
    if not info.get("email_verified") or (settings.allowed_domain and info.get("hd") != settings.allowed_domain):
        return HTMLResponse("Sign-in is restricted to the company Google Workspace.", 403)
    if not db.scalar(select(User).where(User.email == email, User.active)):
        return HTMLResponse(f"{email} has not been added to the tool. Ask an admin.", 403)
    request.session["email"] = email
    return RedirectResponse("/")

@app.get("/auth/dev")   # local testing only; disabled unless DEV_MODE=true
def dev_login(request: Request, email: str):
    if not settings.dev_mode: return HTMLResponse("Not found", 404)
    request.session["email"] = email.lower(); return RedirectResponse("/")

@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear(); return RedirectResponse("/")

@app.post("/api/{action}")
async def api(action: str, request: Request, db=Depends(get_db), u=Depends(current_user)):
    h = HANDLERS.get(action)
    if not h: raise Err("Unknown action", 404)
    try: body = await request.json()
    except Exception: body = {}
    try:
        res = h(db, u, body or {}); db.commit(); return res
    except Exception:
        db.rollback(); raise

@app.get("/")
def index(request: Request):
    if not request.session.get("email"):
        return RedirectResponse("/auth/login" if settings.google_client_id else "/auth/dev?email=" + settings.admin_email)
    return FileResponse(Path(__file__).parent / "static" / "index.html")
