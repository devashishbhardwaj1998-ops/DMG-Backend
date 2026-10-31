import base64, io, json, os, tempfile
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t3.db"); os.environ.setdefault("DEV_MODE", "true"); os.environ.setdefault("ADMIN_EMAIL", "boss@x.com")
from fastapi.testclient import TestClient
from app.main import app

def client(email):
    c = TestClient(app); c.get(f"/auth/dev?email={email}", follow_redirects=False); return c
def call(c, a, **p): return c.post(f"/api/{a}", json=p)
def mk(i, region, ptype="Full", own="Private", d="2026-02-01", **kw): return dict(id=i, name=f"V{i}", region=region, profileType=ptype, updateReason="x", requestDate=d, ownership=own, **kw)
def member(n, e, title, regions, **kw): return dict(name=n, email=e, title=title, regions=regions, **kw)
UPD = dict(ownDiff=3, typeDiff=5, txDiff=1)

def test_gaps_availability_weights_search_exports():
    with TestClient(app):
        boss = client("boss@x.com")
        call(boss, "saveConfig", cmsUrlTemplate="https://cms.example.com/c/{asset_id}")
        regs = call(boss, "team").json()["cfg"]["regions"]
        if "NORCEE" not in regs: assert call(boss, "saveConfig", regions=regs + ["NORCEE"]).status_code == 200
        # ---- "nobody can take these"
        call(boss, "importCsv", rows=[mk("n1", "NORCEE"), mk("n2", "NORCEE"), mk("s1", "SE + LATAM", "Limited")])
        gaps = lambda: {(g["region"], g["profileType"]): g for g in call(boss, "unassignable").json()["groups"]}
        assert gaps()[("NORCEE", "Full")]["kind"] == "gap" and "Nobody is assigned to NORCEE" in gaps()[("NORCEE", "Full")]["reason"]
        call(boss, "saveUser", **member("Fay", "fay@x.com", "Analyst", ["NORCEE"], profileTypes="Limited"))
        assert "no" not in gaps()[("NORCEE", "Full")]["reason"].lower()[:0] and "eligible" in gaps()[("NORCEE", "Full")]["reason"]   # in the region but not eligible for Full
        call(boss, "saveUser", **member("Fay", "fay@x.com", "Analyst", ["NORCEE"], profileTypes="Full"))
        assert ("NORCEE", "Full") not in gaps()                                                       # fixed
        fay = client("fay@x.com")
        assert call(fay, "unassignable").status_code == 403 and call(fay, "whoIsOn").status_code == 403
        # ---- availability blocks Get Next; only admins mark leave
        assert call(fay, "setAvailability", status="Not available", note="Dentist").status_code == 200
        assert call(fay, "getNext").status_code == 400
        assert gaps()[("NORCEE", "Full")]["kind"] == "away"
        assert call(fay, "setAvailability", status="On leave").status_code == 403
        assert call(boss, "setAvailability", user="Fay", status="On leave", note="Holiday").status_code == 200
        assert call(fay, "setAvailability", status="Available").status_code == 403                    # can't clear admin-set leave
        row = next(r for r in call(boss, "whoIsOn").json()["rows"] if r["name"] == "Fay"); assert row["status"] == "On leave" and row["capacity"] == "not taking work" and row["setBy"] == "Boss"
        call(boss, "setAvailability", user="Fay", status="Available")
        notes = call(boss, "notifications").json(); n_gap = [n for n in notes["items"] if n["kind"] == "gap"]
        assert n_gap and len([n for n in call(boss, "notifications").json()["items"] if n["kind"] == "gap"]) == len(n_gap)   # alert raised once per gap per day
        # ---- updater vs reviewer complexity, weighted output (editable)
        call(boss, "saveUser", **member("Gus", "gus@x.com", "Senior Analyst", ["NORCEE"], isReviewer=True)); gus = client("gus@x.com")
        a = call(fay, "getNext").json(); assert a["region"] == "NORCEE"
        assert call(fay, "complete", id=a["id"]).status_code == 400                                    # must record own reading
        assert call(fay, "complete", id=a["id"], ownDiff=9, typeDiff=5, txDiff=1).status_code == 400
        assert call(fay, "complete", id=a["id"], **UPD).status_code == 200
        item = next(x for x in call(gus, "reviewQueue").json()["pool"] if x["id"] == a["id"]); assert item["updReading"] == {"own": 3, "type": 5, "tx": 1, "pct": 60}
        call(gus, "pickReview", id=a["id"])
        assert call(gus, "submitReview", id=a["id"], ownDiff=5, typeDiff=5, txDiff=1, accuracy=4, coverage=4, consistency=5, notesScore=5).status_code == 200
        done = next(x for x in call(gus, "reviewQueue").json()["done"] if x["id"] == a["id"]); assert done["updReading"]["pct"] == 60 and done["reviewReading"]["pct"] == 73
        st = call(boss, "teamStats").json(); r = next(x for x in st["rows"] if x["name"] == "Fay")
        assert r["updates"] == 1 and r["weighted"] == 1.5 and r["compared"] == 1 and r["differ"] == 1 and r["avgGap"] == 13   # Full 1.0 x (1 + 0.5 bonus, reviewer's 73% reading)
        assert next(x for x in call(gus, "teamStats").json()["rows"]) ["name"] == "Gus" and len(call(fay, "teamStats").json()["rows"]) == 1   # non-admins see only themselves
        out = {**st["output"], "profileType": {**st["output"]["profileType"], "Full": 2.0}}
        assert call(fay, "saveConfig", output=out).status_code == 403
        assert call(boss, "saveConfig", output={**out, "bands": [{"upTo": 50, "bonus": 0}]}).status_code == 400   # last band must end at 100
        assert call(boss, "saveConfig", output=out).status_code == 200
        assert next(x for x in call(boss, "teamStats").json()["rows"] if x["name"] == "Fay")["weighted"] == 3.0   # admin edit applies immediately
        # ---- search keeps the region wall
        call(boss, "saveUser", **member("Uma", "uma@x.com", "Analyst", ["The UK"])); call(boss, "importCsv", rows=[mk("uk1", "The UK")])
        assert [x["id"] for x in call(fay, "search", q="uk1").json()["items"]] == []                   # other region: invisible
        assert call(fay, "search", region="The UK").status_code == 403 and call(fay, "search", person="Uma").status_code == 403
        assert [x["id"] for x in call(boss, "search", q="uk1").json()["items"]] == ["uk1"]
        mine = call(fay, "search", q="n1").json(); assert mine["total"] == 1 and mine["items"][0]["cmsUrl"].startswith("https://cms.example.com/c/")
        s2 = call(boss, "search", status="Reviewed", region="NORCEE").json(); assert s2["total"] == 1 and s2["byStatus"] == {"Reviewed": 1}
        assert call(boss, "search", q="Gus").json()["total"] >= 1                                      # admins can search by person
        # ---- exports: admin only, internal columns off by default, saved profiles, log
        meta = call(boss, "exportMeta").json(); assert call(fay, "exportMeta").status_code == 403 and call(fay, "exportRun").status_code == 403
        assert any(c["internal"] for c in meta["columns"]) and not any(c["internal"] for c in meta["columns"] if c["key"] in meta["defaults"])
        r = call(boss, "exportRun", filters={"regions": ["NORCEE"]}).json(); text = base64.b64decode(r["contentBase64"]).decode("utf-8-sig")
        assert r["rows"] == 2 and "QA %" not in text and "Updated by" not in text and text.splitlines()[0].startswith("Asset ID,Asset,Region")
        r2 = call(boss, "exportRun", filters={"regions": ["NORCEE"], "statuses": ["Reviewed"]}, columns=["id", "assignee", "reviewer", "qa", "updDifficulty", "difficulty", "points"]).json()
        assert "Fay" in base64.b64decode(r2["contentBase64"]).decode("utf-8-sig") and r2["rows"] == 1
        assert call(boss, "exportSaveProfile", name="EMEA weekly", filters={"regions": ["NORCEE", "SE + LATAM"]}, columns=["id", "name", "region"], format="xlsx", tabPerRegion=True).status_code == 200
        pid = call(boss, "exportProfiles").json()[0]["id"]; x = call(boss, "exportRun", profileId=pid).json()
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(base64.b64decode(x["contentBase64"]))); assert set(wb.sheetnames) == {"NORCEE", "SE + LATAM"} and x["filename"].endswith(".xlsx")
        lg = call(boss, "exportLog").json(); assert lg[0]["profile"] == "EMEA weekly" and lg[0]["user"] == "Boss" and lg[1]["profile"] == "Ad hoc" and call(fay, "exportLog").status_code == 403
        assert call(boss, "exportDeleteProfile", id=pid).status_code == 200 and call(boss, "exportProfiles").json() == []

def test_other_screens_actions():
    with TestClient(app):
        boss = client("boss@x.com"); call(boss, "saveUser", **member("Kim", "kim@x.com", "Analyst", ["France"])); kim = client("kim@x.com")
        k = call(boss, "kpi", period="week").json(); assert len(k["rows"]) > 1 and len(call(kim, "kpi", period="month").json()["rows"]) == 1
        q = call(boss, "quality").json(); assert "teamAvg" in q and len(call(kim, "quality").json()["rows"]) == 1
        o = call(boss, "overview").json(); assert len(o["trend"]) == 14 and call(kim, "overview").status_code == 403
        assert call(boss, "projectImport", rows=[dict(id="p1", name="Proj A", project="Alpha", priority="High", requestDate="2026-03-01"), dict(id="p2", name="Proj B", project="Alpha")]).json()["added"] == 2
        assert call(boss, "projectAllocate", to="Kim", count=2).json() == 2
        items = call(kim, "projectList").json()["items"]; assert len(items) == 2 and call(kim, "projectImport", rows=[]).status_code == 403
        assert call(kim, "projectStart", pid=items[0]["pid"]).status_code == 200 and call(kim, "projectComplete", pid=items[0]["pid"]).status_code == 200
        assert call(boss, "leaveAdd", user="Kim", **{"from": "2026-12-07", "to": "2026-12-09", "type": "Annual"}).json()["added"] == 3
        lv = call(boss, "leaveView", week="2026-12-07").json(); assert len(lv["leaves"]) == 3 and call(kim, "leaveView", week="2026-12-07").json()["users"] == [{"name": "Kim", "role": "Analyst"}]
        assert call(boss, "leaveDelete", id=lv["leaves"][0]["id"]).status_code == 200
        nt = call(kim, "notifications").json(); assert nt["unread"] >= 1; call(kim, "markRead"); assert call(kim, "notifications", countOnly=True).json()["unread"] == 0
