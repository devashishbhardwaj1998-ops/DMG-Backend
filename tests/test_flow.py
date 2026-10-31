import os, tempfile
os.environ.update(DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db", DEV_MODE="true", ADMIN_EMAIL="boss@x.com")
from fastapi.testclient import TestClient
from app.main import app

def client(email):
    c = TestClient(app); c.get(f"/auth/dev?email={email}", follow_redirects=False); return c
def call(c, a, **p): return c.post(f"/api/{a}", json=p)
def mk(i, region, own="Private", d="2026-01-01", **kw): return dict(id=i, name=f"Co{i}", region=region, profileType="Full", updateReason="x", requestDate=d, ownership=own, **kw)
def member(n, e, title, regions, **kw): return dict(name=n, email=e, title=title, regions=regions, **kw)
UPD = dict(ownDiff=3, typeDiff=5, txDiff=1)
QA = dict(ownDiff=1, typeDiff=5, txDiff=1, accuracy=4, coverage=4, consistency=5, notesScore=5)

def test_region_walls_and_review_pipeline():
    with TestClient(app):
        boss = client("boss@x.com")
        # ---- team: titles, reviewer tag, multi-region
        assert call(boss, "saveUser", **member("Ana", "ana@x.com", "Analyst", ["DACH"])).status_code == 200            # alias DACH ok, DE would also map
        assert call(boss, "saveUser", **member("Ben", "ben@x.com", "Senior Analyst", ["DACH", "France"], isReviewer=True)).status_code == 200
        assert call(boss, "saveUser", **member("Cara", "cara@x.com", "Junior Analyst", ["France"])).status_code == 200
        assert call(boss, "saveUser", **member("Eve", "eve@x.com", "Senior Associate", ["Benelux"], isReviewer=True)).status_code == 200
        assert call(boss, "saveUser", **member("Zed", "zed@x.com", "Associate", [])).status_code == 400                   # non-admin needs a region
        assert call(boss, "saveUser", **member("Zed", "zed@x.com", "Boss", ["DACH"])).status_code == 400                    # unknown title
        assert call(boss, "saveUser", **member("Zed", "zed@x.com", "Analyst", ["Mars"])).status_code == 400                 # unknown region
        t = call(boss, "team").json(); ben = next(u for u in t["users"] if u["name"] == "Ben")
        assert ben["regions"] == ["DACH", "France"] and ben["isReviewer"] and ben["title"] == "Senior Analyst"
        # ---- assets: only allowed regions, aliases map, CMS links
        r = call(boss, "importCsv", rows=[mk("1", "DACH", "Others", "2026-03-01"), mk("2", "DE", "Private Equity Majority", "2026-05-01"), mk("3", "DACH", "Private Equity Majority", "2026-03-01"),
                                          mk("4", "France", "Private", "2026-02-01"), mk("5", "France", "Private", "2026-04-01"), mk("6", "Benelux"), mk("7", "Mars"), mk("8", "SE"), mk("1", "DACH")]).json()
        assert (r["added"], r["dup"], r["invalid"]) == (6, 1, 2) and set(r["invalidRegions"]) == {"Mars", "SE"}
        ana, ben_c, cara, eve = client("ana@x.com"), client("ben@x.com"), client("cara@x.com"), client("eve@x.com")
        # ---- region wall on work: Ana (DACH) only ever receives DACH, in priority order
        got = [call(ana, "getNext").json()["id"] for _ in range(3)]
        assert got == ["3", "2", "1"]                                                      # PE Majority (oldest), PE Majority, Others; all DACH
        assert call(ana, "getNext").status_code == 400                                     # DACH is empty now: she must NOT get France/Benelux assets
        assert call(cara, "getNext").json()["id"] == "4"                                   # Cara (France) gets France only
        assert call(ana, "dashboard").json()["available"] == 0                             # no leakage of other regions' counts
        assert call(ana, "getNext", region="France").status_code == 403                    # can't even ask for another region
        assert call(ana, "pipeline").status_code == 403 and call(ana, "team").status_code == 403
        # ---- admin reassign obeys the region rule (no override)
        assert call(boss, "reassign", id="5", to="Ana").status_code == 400                 # Ana isn't in France
        assert call(boss, "reassign", id="5", to="Ben", override=True, reason="x").status_code == 200   # Ben is in France
        # ---- review pipeline: reviewer tag + region + not your own work
        for aid in ("3", "2"): call(ana, "start", id=aid); assert call(ana, "complete", id=aid, **UPD).status_code == 200
        assert call(cara, "reviewQueue").status_code == 403                                # no reviewer tag, no access
        assert call(eve, "reviewQueue").json()["pool"] == []                               # Benelux reviewer sees no DACH assets
        assert call(eve, "pickReview", id="3").status_code == 403
        q = call(ben_c, "reviewQueue").json(); assert sorted(a["id"] for a in q["pool"]) == ["2", "3"]
        assert call(ben_c, "pickReview", id="3").status_code == 200
        assert call(ben_c, "pickReview", id="3").status_code == 400                        # already picked
        assert call(boss, "reviewQueue").json()["claimed"][0]["reviewer"] == "Ben"        # admin sees everything
        assert call(ben_c, "submitReview", id="2", **QA).status_code == 403               # must pick first
        assert call(ben_c, "submitReview", id="3", **{**QA, "accuracy": 9}).status_code == 400
        assert call(ben_c, "submitReview", id="3", **QA).json() == 78
        # a reviewer can't review their own update
        call(boss, "addAsset", id="9", name="Co9", region="DACH", ownership="Private", profileType="Full")
        b = call(ben_c, "getNext").json(); call(ben_c, "complete", id=b["id"], **UPD)
        assert call(ben_c, "pickReview", id=b["id"]).status_code == 403
        assert call(boss, "assignReview", id="2", reviewer="Eve").status_code == 400      # Eve is Benelux, asset is DACH
        assert call(boss, "assignReview", id="2", reviewer="Cara").status_code == 400     # no reviewer tag
        assert call(boss, "assignReview", id="2", reviewer="Ben").status_code == 200
        assert call(ben_c, "releaseReview", id="2").status_code == 200 and call(ben_c, "reviewQueue").json()["pool"]
        assert call(ana, "assignReview", id="2", reviewer="Ben").status_code == 403       # only admins assign
        # ---- admin region filter + holding list
        assert {a["region"] for a in call(boss, "pipeline", region="Benelux").json()["queue"]} == {"Benelux"}
        assert call(ben_c, "reviewQueue", region="Benelux").status_code == 403

def test_cms_link_and_settings():
    with TestClient(app):
        boss = client("boss@x.com"); call(boss, "saveUser", **member("Dee", "dee@x.com", "Analyst", ["The UK"]))
        call(boss, "importCsv", rows=[mk("c1", "The UK"), mk("c2", "The UK", cmsUrl="https://cms.example.com/special/c2"), mk("c3 x/y", "The UK"), mk("c4", "The UK", cmsUrl="javascript:alert(1)")])
        dee = client("dee@x.com"); assert call(dee, "pipeline").status_code == 403
        assert call(boss, "saveConfig", cmsUrlTemplate="javascript:{asset_id}").status_code == 400
        assert call(boss, "saveConfig", cmsUrlTemplate="https://cms.example.com/company/").status_code == 400    # needs {asset_id}
        assert call(boss, "saveConfig", cmsUrlTemplate="https://cms.example.com/company/{asset_id}").status_code == 200
        links = {a["id"]: a["cmsUrl"] for a in call(boss, "pipeline", region="The UK").json()["queue"]}
        assert links["c1"] == "https://cms.example.com/company/c1" and links["c2"] == "https://cms.example.com/special/c2"
        assert links["c3 x/y"] == "https://cms.example.com/company/c3%20x%2Fy" and links["c4"].startswith("https://cms.example.com/company/")   # unsafe per-asset URL ignored
        a = call(dee, "getNext").json(); assert a["cmsUrl"].startswith("https://cms.example.com/")                    # link reaches the analyst's own row
        assert call(boss, "saveConfig", regions=["The US", "The UK", "France", "DACH", "Benelux", "SE + LATAM"]).status_code == 200   # NORCEE unused: removable
        assert call(boss, "saveConfig", regions=["The US", "France", "DACH", "Benelux", "SE + LATAM"]).status_code == 400            # The UK in use
        assert call(boss, "saveUser", **member("Eli", "eli@x.com", "Analyst", ["NORCEE"])).status_code == 400                       # removed region now rejected
        assert call(boss, "saveUser", name="Boss", email="boss@x.com", title="Senior Associate", isAdmin=False, regions=["DACH"]).status_code == 400   # can't demote self

def test_team_csv_and_auth():
    with TestClient(app):
        boss = client("boss@x.com")
        rows = [dict(name="Gia", email="gia@x.com", roles="Senior Analyst, Reviewer", region="DACH; France", eligibility="Full, Limited"),
                dict(name="Hal", email="HAL@x.com", roles="Associate", region="UK", eligibility="All"),
                dict(name="Bad", email="bad@x.com", roles="Analyst", region="DACH", eligibility="France"),       # region in Eligibility
                dict(name="Bad2", email="bad2@x.com", roles="Analyst, Associate", region="DACH"),                  # two titles
                dict(name="Bad3", email="bad3@x.com", roles="Analyst", region=""),                                 # no region
                dict(name="Dup", email="gia@x.com", roles="Analyst", region="DACH")]
        pv = call(boss, "previewTeamImport", rows=rows).json(); assert [x["action"] for x in pv] == ["add", "add", "error", "error", "error", "error"]
        assert pv[0]["title"] == "Senior Analyst" and pv[0]["isReviewer"] and pv[0]["regions"] == ["DACH", "France"] and pv[1]["regions"] == ["The UK"]
        n = len(call(boss, "team").json()["users"]); assert call(boss, "team").json() and n == len(call(boss, "team").json()["users"])   # preview wrote nothing
        r = call(boss, "importTeam", rows=rows).json(); assert (r["added"], r["updated"], len(r["errors"])) == (2, 0, 4)
        gia = next(u for u in call(boss, "team").json()["users"] if u["email"] == "gia@x.com"); assert gia["regions"] == ["DACH", "France"] and gia["profileTypes"] == "Full, Limited"
        assert call(client("gia@x.com"), "importTeam", rows=rows).status_code == 403
        assert call(boss, "audit").json()[0]["action"] == "Team CSV Import"
        assert TestClient(app).post("/api/dashboard", json={}).status_code == 401                                 # no session = no access
        assert client("stranger@x.com").post("/api/dashboard", json={}).status_code == 401                        # not in the Users table
