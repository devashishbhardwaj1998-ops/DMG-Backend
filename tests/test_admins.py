import os, tempfile
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/ta.db"); os.environ.setdefault("DEV_MODE", "true"); os.environ.setdefault("ADMIN_EMAIL", "boss@x.com")
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app, seed_admins
from app.db import SessionLocal
from app.models import User

def test_multiple_admins_all_regions():
    with TestClient(app):
        with SessionLocal() as db:
            seed_admins(db, ["hardik.maheshwari@gain.ai", "devashish.bhardwaj@gain.ai"])
            seed_admins(db, ["hardik.maheshwari@gain.ai", "devashish.bhardwaj@gain.ai"])    # running again must not duplicate
            admins = {u.email: u for u in db.scalars(select(User).where(User.email.like("%@gain.ai")))}
        assert len(admins) == 2 and admins["devashish.bhardwaj@gain.ai"].name == "Devashish Bhardwaj" and admins["hardik.maheshwari@gain.ai"].is_admin
        c = TestClient(app); c.get("/auth/dev?email=devashish.bhardwaj@gain.ai", follow_redirects=False)
        me = c.post("/api/boot", json={}).json()["me"]
        assert me["isAdmin"] and me["isReviewer"] and len(me["regions"]) == 7                  # admin = every region
        assert c.post("/api/pipeline", json={}).status_code == 200 and c.post("/api/team", json={}).status_code == 200
        assert c.post("/api/pipeline", json={"region": "France"}).status_code == 200            # and can narrow to any one region
