"""Business logic. Every handler receives (db, user, payload) and enforces its own permissions."""
import base64
import csv
import io
import json
import re
from urllib.parse import quote
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import case, func, or_, select
from .config import settings
from .models import User, UserRegion, Asset, Audit, Config, Project, Leave, Notification, ExportProfile, ExportLog

DEF_ORDER = ["Private Equity Majority", "Private Equity Minority", "VC-backed", "Private", "Others"]
DEF_QA = {
 "scale": {"ownDiff": 5, "typeDiff": 5, "txDiff": 5, "accuracy": 5, "coverage": 5, "consistency": 5, "notesScore": 5},
 "weights": {"difficulty": 30, "accuracy": 10, "coverage": 20, "consistency": 20, "notesScore": 20},  # PLACEHOLDER - confirm vs sheet
 "ownershipScore": {"Private Equity Majority": 3, "Private Equity Minority": 3, "VC-backed": 3, "Private": 1, "Others": 5},
 "typeScore": {"Full": 5, "Limited": 3, "Minimal": 1},
 "dealsScore": {"0 deals": 1, "More than 1 and less than 3 deals": 3, "More than 3 and less than 5 deals": 5}}
ACTIVE = ("Assigned", "In Progress")
TITLES = ["Junior Analyst", "Analyst", "Senior Analyst", "Associate", "Senior Associate"]
TITLE_ALIASES = {"junior analyst": "Junior Analyst", "jr analyst": "Junior Analyst", "jr. analyst": "Junior Analyst", "analyst": "Analyst",
                 "senior analyst": "Senior Analyst", "sr analyst": "Senior Analyst", "sr. analyst": "Senior Analyst", "associate": "Associate",
                 "senior associate": "Senior Associate", "sr associate": "Senior Associate", "sr. associate": "Senior Associate"}
DEF_REGIONS = ["The US", "The UK", "France", "DACH", "Benelux", "SE + LATAM", "NORCEE"]
REGION_ALIASES = {  # country codes / names accepted on import; a bare "SE" is NOT accepted (Sweden vs Southern Europe)
 "US": "The US", "USA": "The US", "UNITED STATES": "The US", "UK": "The UK", "GB": "The UK", "GBR": "The UK", "UNITED KINGDOM": "The UK",
 "FR": "France", "FRA": "France", "DE": "DACH", "CH": "DACH", "AT": "DACH", "GERMANY": "DACH", "SWITZERLAND": "DACH", "AUSTRIA": "DACH",
 "BE": "Benelux", "NL": "Benelux", "LU": "Benelux", "BLX": "Benelux", "BELGIUM": "Benelux", "NETHERLANDS": "Benelux", "LUXEMBOURG": "Benelux",
 "ES": "SE + LATAM", "IT": "SE + LATAM", "PT": "SE + LATAM", "GR": "SE + LATAM", "LATAM": "SE + LATAM", "BR": "SE + LATAM", "MX": "SE + LATAM",
 "AR": "SE + LATAM", "CL": "SE + LATAM", "CO": "SE + LATAM", "PE": "SE + LATAM", "SPAIN": "SE + LATAM", "ITALY": "SE + LATAM", "PORTUGAL": "SE + LATAM",
 "NO": "NORCEE", "DK": "NORCEE", "FI": "NORCEE", "PL": "NORCEE", "CZ": "NORCEE", "HU": "NORCEE", "RO": "NORCEE", "NORWAY": "NORCEE", "DENMARK": "NORCEE", "FINLAND": "NORCEE", "POLAND": "NORCEE"}
PROFILE_TYPES = ["Full", "Limited", "Minimal"]

class Err(Exception):
    def __init__(self, msg, code=400): self.msg, self.code = msg, code

def now(): return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)
def ts(x): return x.strftime("%Y-%m-%d %H:%M:%S") if x else ""
def need(ok):
    if not ok: raise Err("Permission denied", 403)
is_admin = lambda u: bool(u.is_admin)
is_rev = lambda u: bool(u.is_admin or u.is_reviewer)

def cfg(db):
    o = {c.key: c.value for c in db.scalars(select(Config))}
    q = json.loads(o["qa"]) if "qa" in o else {}
    return {"maxPipeline": int(o.get("maxPipeline", 5)), "overdueHours": int(o.get("overdueHours", 48)),
            "reviewSlaHours": int(o.get("reviewSlaHours", 24)), "weekendLock": o.get("weekendLock", "false") == "true",
            "ownershipOrder": json.loads(o["ownershipOrder"]) if "ownershipOrder" in o else DEF_ORDER,
            "output": ({**DEF_OUTPUT, **json.loads(o["output"])} if "output" in o else json.loads(json.dumps(DEF_OUTPUT))),
            "regions": json.loads(o["regions"]) if "regions" in o else list(DEF_REGIONS), "cmsUrlTemplate": o.get("cmsUrlTemplate", ""), "titles": TITLES,
            "qa": {"scale": {**DEF_QA["scale"], **q.get("scale", {})}, "weights": {**DEF_QA["weights"], **q.get("weights", {})},
                   "ownershipScore": q.get("ownershipScore", DEF_QA["ownershipScore"]), "typeScore": q.get("typeScore", DEF_QA["typeScore"]),
                   "dealsScore": q.get("dealsScore", DEF_QA["dealsScore"])}}

def set_cfg(db, k, v):
    r = db.get(Config, k)
    v = v if isinstance(v, str) and k in ("weekendLock", "cmsUrlTemplate") else json.dumps(v)
    if r: r.value = v
    else: db.add(Config(key=k, value=v))

def log(db, user, action, ref="", detail=""):
    db.add(Audit(ts=now(), user_name=user.name, action=action, asset_ref=str(ref), detail=detail))

def norm_region(allowed, r):
    t = " ".join(str(r or "").split())
    if not t: return None
    hit = next((x for x in allowed if x.lower() == t.lower()), None)
    if hit: return hit
    alias = REGION_ALIASES.get(t.upper())
    return next((x for x in allowed if alias and x.lower() == alias.lower()), None)

def regions_from(allowed, val):
    items = val if isinstance(val, list) else re.split(r"[,;|]+", str(val or ""))
    res = []
    for x in items:
        if not str(x).strip(): continue
        n = norm_region(allowed, x)
        if not n: raise Err(f'Region "{str(x).strip()}" is not allowed. Use: {", ".join(allowed)}')
        if n not in res: res.append(n)
    return res

def work_regions(u): return list(u.regions)   # regions this person pulls work from (admins too)

def scope(db, u, requested=None):
    """Regions this user may SEE. Admins: every region. Everyone else: only their own. A requested filter must be inside that set."""
    allowed = cfg(db)["regions"] if is_admin(u) else list(u.regions)
    if requested:
        r = norm_region(allowed, requested)
        if not r: raise Err("You don't have access to that region", 403)
        return [r]
    return allowed

def http_url(x):
    x = str(x or "").strip()
    return x if x.lower().startswith(("http://", "https://")) and len(x) <= 500 else ""

def cms_link(a, tpl):
    return http_url(a.cms_url) or (http_url(tpl.replace("{asset_id}", quote(a.asset_id, safe=""))) if tpl else "")

def reading(a, kind):
    o, t, x, p = (a.upd_own_diff, a.upd_type_diff, a.upd_tx_diff, a.upd_difficulty_pct) if kind == "upd" else (a.own_diff, a.type_diff, a.tx_diff, a.difficulty_pct)
    return None if o is None else {"own": o, "type": t, "tx": x, "pct": p}

def out(a, tpl=""):
    n = lambda v: "" if v is None else v
    return {"id": a.asset_id, "name": a.name, "region": a.region, "profileType": a.profile_type, "updateReason": a.update_reason,
            "requestDate": a.request_date.isoformat() if a.request_date else "", "ownership": a.ownership, "source": a.source, "status": a.status,
            "assignee": a.assignee.name if a.assignee else "", "assignedAt": ts(a.assigned_at), "startedAt": ts(a.started_at), "completedAt": ts(a.completed_at),
            "reviewer": a.reviewer.name if a.reviewer else "", "reviewStatus": a.review_status, "reviewDate": ts(a.review_date),
            "difficulty": n(a.difficulty_pct), "percentage": n(a.qa_pct), "cmsNotes": a.cms_notes, "comments": a.comments, "cmsUrl": cms_link(a, tpl),
            "updReading": reading(a, "upd"), "reviewReading": reading(a, "rev")}

def csv_list(x, upper=False):
    return [i.strip().upper() if upper else i.strip().lower() for i in str(x or "").split(",") if i.strip()]

def queue_stmt(order, user=None, regions=None):
    rank = case({o: i for i, o in enumerate(order)}, value=Asset.ownership, else_=len(order))
    st = select(Asset).where(Asset.status == "Available")
    if regions is not None: st = st.where(Asset.region.in_(list(regions)))   # region wall
    if user is not None:
        types = csv_list(user.profile_types)
        if types: st = st.where(func.lower(Asset.profile_type).in_(types))   # profile-type eligibility
    # ownership priority -> oldest Request update from -> Asset ID (numeric-ish)
    return st.order_by(rank, Asset.request_date.is_(None), Asset.request_date, func.length(Asset.asset_id), Asset.asset_id)

def notify(db, uid, kind, msg, ref=""):
    if uid: db.add(Notification(user_id=uid, ts=now(), kind=kind, message=msg, ref=ref, read=False))
def cnt(db, model, *cond): return db.scalar(select(func.count()).select_from(model).where(*cond))
def visible_users(db, u):   # admins see the team, everyone else only themselves
    return db.scalars(select(User).where(User.active).order_by(User.name)).all() if is_admin(u) else [u]

def active_count(db, uid):
    return db.scalar(select(func.count()).select_from(Asset).where(Asset.assignee_id == uid, Asset.status.in_(ACTIVE)))
def limit_for(db, u): return u.max_pipeline or cfg(db)["maxPipeline"]
def by_asset_id(db, aid):
    a = db.scalar(select(Asset).where(Asset.asset_id == str(aid)))
    if not a: raise Err("Asset not found", 404)
    return a
def own_asset(db, u, aid):
    a = by_asset_id(db, aid)
    if a.assignee_id != u.id: raise Err("This asset is not assigned to you", 403)
    return a
def pdate(s):
    if not s: return None
    if isinstance(s, date): return s
    try: return date.fromisoformat(str(s)[:10])
    except ValueError: return None

def calc_qa(q, s):
    sc, w = q["scale"], q["weights"]
    d = (s["ownDiff"] / sc["ownDiff"] + s["typeDiff"] / sc["typeDiff"] + s["txDiff"] / sc["txDiff"]) / 3   # sheet: (1+5+1)/15 = 47%
    parts = {"difficulty": d, "accuracy": s["accuracy"] / sc["accuracy"], "coverage": s["coverage"] / sc["coverage"],
             "consistency": s["consistency"] / sc["consistency"], "notesScore": s["notesScore"] / sc["notesScore"]}
    tw = sum(float(w.get(k, 0)) for k in parts)
    pct = sum(float(w.get(k, 0)) * v for k, v in parts.items()) / tw if tw else 0
    return round(d * 100), round(pct * 100)

# ---------------- handlers ----------------
def boot(db, u, p):
    return {"me": {"name": u.name, "email": u.email, "title": u.title, "role": u.role, "isAdmin": bool(u.is_admin), "isReviewer": bool(u.is_reviewer or u.is_admin), "regions": scope(db, u), "availability": u.availability or "Available", "availabilityNote": u.availability_note or ""}, "cfg": cfg(db)}

def dashboard(db, u, p):   # everyone only ever receives their own rows
    tpl, regs = cfg(db)["cmsUrlTemplate"], work_regions(u)
    mine = db.scalars(select(Asset).where(Asset.assignee_id == u.id, Asset.status.in_(ACTIVE)).order_by(Asset.assigned_at)).all()
    qa = db.scalars(select(Asset).where(Asset.assignee_id == u.id, Asset.qa_pct.is_not(None)).order_by(Asset.review_date.desc()).limit(50)).all()
    return {"mine": [out(a, tpl) for a in mine], "max": limit_for(db, u), "regions": regs, "noRegion": not regs,
            "done": cnt(db, Asset, Asset.assignee_id == u.id, Asset.completed_at.is_not(None)),
            "pendingReviews": cnt(db, Asset, Asset.reviewer_id == u.id, Asset.status == "Pending Review"),
            "available": cnt(db, Asset, Asset.status == "Available", Asset.region.in_(regs)) if regs else 0,
            "qa": [{"name": a.name, "pct": a.qa_pct, "notes": a.comments} for a in qa]}

def getNext(db, u, p):
    db.execute(select(User).where(User.id == u.id).with_for_update()).scalar_one()   # serialise double-clicks per user
    c = cfg(db); today = now().date()
    regs = work_regions(u)
    if not regs: raise Err("No region is assigned to you yet. Ask an admin.", 403)
    if p.get("region"):
        r = norm_region(regs, p["region"])
        if not r: raise Err("You don't have access to that region", 403)
        regs = [r]
    if c["weekendLock"] and today.weekday() >= 5: raise Err("Assignments are locked on weekends.")
    if db.scalar(select(Leave.id).where(Leave.user_id == u.id, Leave.day == today)): raise Err("You are marked on leave today.")
    if (u.availability or "Available") != "Available": raise Err(f"You are set to '{u.availability}'. Set yourself Available to receive work.")
    mx = limit_for(db, u)
    if active_count(db, u.id) >= mx: raise Err(f"Pipeline full ({mx}/{mx}). Complete an asset first.")
    a = db.scalars(queue_stmt(c["ownershipOrder"], u, regs).limit(1).with_for_update(skip_locked=True)).first()  # same region only; no double assignment
    if not a:
        raise Err("No assets in your region(s) match your profile-type eligibility right now." if cnt(db, Asset, Asset.status == "Available", Asset.region.in_(work_regions(u))) else "No assets available in your region(s).")
    a.status, a.assignee_id, a.assigned_at = "Assigned", u.id, now()
    log(db, u, "Get Next", a.asset_id, f"{a.name} [{a.region} / {a.ownership}]")
    notify(db, u.id, "assignment", f"{a.name} assigned to you", a.asset_id)
    db.flush(); return out(a, c["cmsUrlTemplate"])

def start(db, u, p):
    a = own_asset(db, u, p["id"]); a.status = "In Progress"; a.started_at = a.started_at or now()
    log(db, u, "Started", a.asset_id, a.name); return 1

def complete(db, u, p):
    a = own_asset(db, u, p["id"])
    if a.status not in ACTIVE: raise Err("Asset is not active")
    sc = cfg(db)["qa"]["scale"]; v = {}
    for k in ("ownDiff", "typeDiff", "txDiff"):   # the updater re-enters ownership / type / deals so the reviewer's reading can be compared with it
        try: n = int(p.get(k))
        except (TypeError, ValueError): n = 0
        if not 1 <= n <= sc[k]: raise Err(f"Record your difficulty reading first: {k} must be between 1 and {sc[k]}")
        v[k] = n
    a.upd_own_diff, a.upd_type_diff, a.upd_tx_diff = v["ownDiff"], v["typeDiff"], v["txDiff"]
    a.upd_difficulty_pct = round((v["ownDiff"] / sc["ownDiff"] + v["typeDiff"] / sc["typeDiff"] + v["txDiff"] / sc["txDiff"]) / 3 * 100)
    a.started_at = a.started_at or now(); a.completed_at = now(); a.status = "Pending Review"; a.review_status = "Pending"
    log(db, u, "Marked Complete", a.asset_id, f"Moved to Update Review (updater difficulty {a.upd_difficulty_pct}%)"); return 1

def reviewQueue(db, u, p):
    """Review pipeline. Only reviewer-tagged people (and admins) get in, and only for their own regions."""
    need(is_rev(u)); c = cfg(db); tpl = c["cmsUrlTemplate"]; regs = scope(db, u, p.get("region"))
    base = select(Asset).where(Asset.region.in_(regs))
    pending = db.scalars(base.where(Asset.status == "Pending Review").order_by(Asset.completed_at)).all()
    done_q = base.where(Asset.review_status == "Completed").order_by(Asset.review_date.desc()).limit(50)
    if not is_admin(u): done_q = done_q.where(Asset.reviewer_id == u.id)
    pool = [a for a in pending if a.reviewer_id is None and (is_admin(u) or a.assignee_id != u.id)]   # can't pick your own update
    claimed = [a for a in pending if a.reviewer_id is not None and (is_admin(u) or a.reviewer_id == u.id)]
    revs = db.scalars(select(User).where(User.active, or_(User.is_admin, User.is_reviewer)).order_by(User.name)).all() if is_admin(u) else []
    return {"pool": [out(a, tpl) for a in pool], "claimed": [out(a, tpl) for a in claimed], "done": [out(a, tpl) for a in db.scalars(done_q)],
            "qa": c["qa"], "regions": regs,
            "reviewers": [{"name": r.name, "regions": r.regions, "isAdmin": bool(r.is_admin)} for r in revs]}

def pickReview(db, u, p):
    need(is_rev(u)); a = by_asset_id(db, p["id"])
    if a.region not in scope(db, u): raise Err("That asset is outside your regions", 403)
    if a.status != "Pending Review": raise Err("Asset is not awaiting review")
    if a.reviewer_id: raise Err(f"Already picked by {a.reviewer.name}")
    if a.assignee_id == u.id: raise Err("You can't review your own update", 403)
    a.reviewer_id, a.review_status = u.id, "In Review"
    log(db, u, "Picked Review", a.asset_id, a.name); return 1

def releaseReview(db, u, p):
    need(is_rev(u)); a = by_asset_id(db, p["id"])
    if a.status != "Pending Review" or not a.reviewer_id: raise Err("Nothing to release")
    if not is_admin(u) and a.reviewer_id != u.id: raise Err("Not your review", 403)
    a.reviewer_id, a.review_status = None, "Pending"
    log(db, u, "Released Review", a.asset_id, a.name); return 1

def assignReview(db, u, p):   # admins only; reviewers pick for themselves with pickReview
    need(is_admin(u)); a = by_asset_id(db, p["id"])
    if a.status != "Pending Review": raise Err("Asset is not awaiting review")
    r = db.scalar(select(User).where(User.name == p["reviewer"], User.active))
    if not r or not (r.is_reviewer or r.is_admin): raise Err("That person doesn't have the reviewer tag")
    if not r.is_admin and a.region not in r.regions: raise Err(f"{r.name} isn't assigned to region {a.region}")
    if a.assignee_id == r.id: raise Err("A reviewer can't review their own update")
    old = a.reviewer.name if a.reviewer else "-"; a.reviewer_id, a.review_status = r.id, "Assigned"
    log(db, u, "Assigned Review", a.asset_id, f"{old} -> {r.name}"); notify(db, r.id, "review", f"Review assigned: {a.name}", a.asset_id); return 1

def submitReview(db, u, p):
    need(is_rev(u)); a = by_asset_id(db, p["id"])
    if a.status != "Pending Review": raise Err("Asset is not awaiting review")
    if a.assignee_id == u.id: raise Err("You can't review your own update", 403)
    if not is_admin(u) and (a.reviewer_id != u.id or a.region not in scope(db, u)): raise Err("Pick this review first (it must be in your region)", 403)
    q, sc = cfg(db)["qa"], {}
    for k in ("ownDiff", "typeDiff", "txDiff", "accuracy", "coverage", "consistency", "notesScore"):
        try: n = int(p.get(k))
        except (TypeError, ValueError): n = 0
        if not 1 <= n <= q["scale"][k]: raise Err(f"{k} must be between 1 and {q['scale'][k]}")
        sc[k] = n
    a.own_diff, a.type_diff, a.tx_diff, a.accuracy, a.coverage, a.consistency, a.notes_score = (sc[k] for k in ("ownDiff", "typeDiff", "txDiff", "accuracy", "coverage", "consistency", "notesScore"))
    a.difficulty_pct, a.qa_pct = calc_qa(q, sc)
    a.cms_notes, a.comments = p.get("cmsNotes", ""), p.get("comments", "")
    a.review_status, a.review_date, a.status = "Completed", now(), "Reviewed"
    a.reviewer_id = a.reviewer_id or u.id
    log(db, u, "QA Score", a.asset_id, f"{a.qa_pct}% (difficulty {a.difficulty_pct}%)")
    notify(db, a.assignee_id, "qa", f"QA score for {a.name}: {a.qa_pct}%", a.asset_id); return a.qa_pct

def pipeline(db, u, p):
    need(is_admin(u)); c = cfg(db); regs = scope(db, u, p.get("region"))
    counts = dict(db.execute(select(Asset.status, func.count()).where(Asset.region.in_(regs)).group_by(Asset.status)).all())
    unassigned = cnt(db, Asset, Asset.status == "Available", Asset.region.not_in(c["regions"]))   # holding list: region missing/unknown
    return {"queue": [out(a, c["cmsUrlTemplate"]) for a in db.scalars(queue_stmt(c["ownershipOrder"], None, regs).limit(5000))],
            "total": sum(counts.values()), "counts": counts, "regions": regs, "holding": unassigned}

def team(db, u, p):
    need(is_admin(u)); res = []
    for x in db.scalars(select(User).order_by(User.name)):
        done = cnt(db, Asset, Asset.assignee_id == x.id, Asset.completed_at.is_not(None))
        res.append({"name": x.name, "email": x.email, "title": x.title, "role": x.role, "isAdmin": bool(x.is_admin), "isReviewer": bool(x.is_reviewer), "regions": x.regions,
                    "profileTypes": x.profile_types, "active": x.active, "maxPipeline": x.max_pipeline or "", "activeCount": active_count(db, x.id), "doneCount": done})
    return {"users": res, "cfg": cfg(db)}

def addAsset(db, u, p):
    need(is_admin(u)); c = cfg(db)
    if not p.get("id") or not p.get("name"): raise Err("Asset ID and name required")
    region = norm_region(c["regions"], p.get("region"))
    if not region: raise Err(f'Region must be one of: {", ".join(c["regions"])}')
    if db.scalar(select(Asset.id).where(Asset.asset_id == str(p["id"]))): raise Err("Asset ID already exists")
    db.add(Asset(asset_id=str(p["id"]), name=p["name"], region=region, profile_type=p.get("profileType", ""), update_reason=p.get("updateReason", ""),
                 request_date=pdate(p.get("requestDate")), ownership=p.get("ownership") or "Others", source="Manual", cms_url=http_url(p.get("cmsUrl"))))
    log(db, u, "Added Asset (manual)", p["id"], f'{p["name"]} [{region}]'); return 1

def importCsv(db, u, p):
    need(is_admin(u)); rows = p.get("rows", []); regions = cfg(db)["regions"]
    if len(rows) > 20000: raise Err("Too many rows (max 20000 per upload)")
    have = set(db.scalars(select(Asset.asset_id))); added = dup = invalid = 0; bad = set()
    for r in rows:
        aid = str(r.get("id", "")).strip()
        if not aid: continue
        region = norm_region(regions, r.get("region"))
        if not region: invalid += 1; bad.add(str(r.get("region") or "").strip() or "(blank)"); continue
        if aid in have: dup += 1; continue
        have.add(aid); added += 1
        db.add(Asset(asset_id=aid, name=r.get("name", ""), region=region, profile_type=r.get("profileType", ""), update_reason=r.get("updateReason", ""),
                     request_date=pdate(r.get("requestDate")), ownership=r.get("ownership") or "Others", source="Morning CSV", cms_url=http_url(r.get("cmsUrl"))))
    log(db, u, "CSV Import", "", f"{added} new, {dup} duplicates ignored, {invalid} skipped (region not allowed)")
    return {"added": added, "dup": dup, "invalid": invalid, "invalidRegions": sorted(bad)[:8]}

def reassign(db, u, p):
    need(is_admin(u)); a = by_asset_id(db, p["id"]); old = a.assignee.name if a.assignee else "Available"; tgt = p.get("to") or ""
    note = ""
    if tgt:
        t = db.scalar(select(User).where(User.name == tgt, User.active))
        if not t: raise Err("User not found")
        if a.region not in t.regions:   # hard rule: assets only go to people in the asset's region (no override)
            raise Err(f"{tgt} isn't assigned to region {a.region}. Add the region to their profile first.")
        if active_count(db, t.id) >= limit_for(db, t) and a.assignee_id != t.id:
            if not p.get("override"): raise Err(f"{tgt} is at the limit. Tick override and give a reason.")
            if not p.get("reason"): raise Err("Override reason required")
            note = f" | override: {p['reason']}"
        a.assignee_id, a.status, a.assigned_at = t.id, "Assigned", now()
    else: a.assignee_id, a.status, a.assigned_at = None, "Available", None
    a.started_at = None
    prev = db.scalar(select(User).where(User.name == old)) if old != "Available" else None
    if prev and prev.name != tgt: notify(db, prev.id, "removed", f"{a.name} was removed from your queue by {u.name}", a.asset_id)
    if tgt: notify(db, a.assignee_id, "assignment", f"{a.name} assigned to you by {u.name}", a.asset_id)
    log(db, u, "Reassigned" if tgt else "Unassigned", a.asset_id, f"{old} -> {tgt or 'Available'}{note}"); return 1

def _types_from(c, val):
    allowed = list(dict.fromkeys(PROFILE_TYPES + list(c["qa"]["typeScore"])))
    res = []
    for t in re.split(r"[,;|]+", str(val or "")):
        t = t.strip()
        if not t or t.lower() in ("all", "any", "none"): continue
        hit = next((x for x in allowed if x.lower() == t.lower()), None)
        if not hit: raise Err(f'Unknown profile type "{t}". Use: {", ".join(allowed)}')
        if hit not in res: res.append(hit)
    return res

def _other_admins(db, uid): return cnt(db, User, User.is_admin, User.active, User.id != uid)

def saveUser(db, u, p):
    need(is_admin(u)); c = cfg(db); email = str(p.get("email", "")).strip().lower()
    if "@" not in email or not p.get("name"): raise Err("Name and a valid email are required")
    title = p.get("title") or "Analyst"
    if title not in TITLES: raise Err("Title must be one of: " + ", ".join(TITLES))
    regs = regions_from(c["regions"], p.get("regions"))
    adm, rev, active = bool(p.get("isAdmin")), bool(p.get("isReviewer")), p.get("active", True) is not False
    if not adm and not regs: raise Err("Assign at least one region (only admins can have none)")
    types = _types_from(c, p.get("profileTypes"))
    x = db.scalar(select(User).where(User.email == email)); new = x is None
    if not new and x.is_admin and (not adm or not active):
        if x.id == u.id: raise Err("You can't remove your own Admin access or deactivate yourself")
        if _other_admins(db, x.id) < 1: raise Err("There must always be at least one active admin")
    if new: x = User(email=email); db.add(x)
    x.name, x.title, x.is_admin, x.is_reviewer, x.active = p["name"], title, adm, rev, active
    x.profile_types = ", ".join(types); x.set_regions(regs)
    x.max_pipeline = int(p["maxPipeline"]) if str(p.get("maxPipeline") or "").isdigit() else None
    log(db, u, "Added Member" if new else "Updated Member", email, f"{p['name']} / {title}{' / Reviewer' if rev else ''}{' / Admin' if adm else ''} / {', '.join(regs) or 'all regions'}"); return 1

def _eval_team(db, actor, rows):
    c = cfg(db); users = {x.email.lower(): x for x in db.scalars(select(User))}; seen = {}; res = []
    for i, r in enumerate(rows):
        o = {"rowNo": i + 2, "name": str(r.get("name") or "").strip(), "email": str(r.get("email") or "").strip().lower(), "title": "", "isReviewer": False,
             "isAdmin": False, "regions": [], "profileTypes": "", "action": "error", "message": ""}
        def fail(m): return {**o, "action": "error", "message": m}
        if not o["name"]: res.append(fail("Name is required")); continue
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", o["email"]): res.append(fail("A valid email is required")); continue
        if o["email"] in seen: res.append(fail(f"Duplicate email (already used in row {seen[o['email']]})")); continue
        seen[o["email"]] = o["rowNo"]
        titles, rev, adm, bad = [], False, False, None
        for t in [x.strip() for x in re.split(r"[,;|/]+", str(r.get("roles") or "")) if x.strip()]:
            k = " ".join(t.lower().split())
            if k in TITLE_ALIASES: titles.append(TITLE_ALIASES[k])
            elif k == "reviewer": rev = True
            elif k in ("admin", "administrator"): adm = True
            else: bad = t; break
        if bad: res.append(fail(f'Unknown role "{bad}". Use a title ({", ".join(TITLES)}), Reviewer and/or Admin')); continue
        if len(set(titles)) > 1: res.append(fail("Only one title is allowed per person")); continue
        ex = users.get(o["email"])
        if not titles and not rev and not adm: res.append(fail("Assigned Roles is required")); continue
        o["title"] = titles[0] if titles else (ex.title if ex else "Analyst"); o["isReviewer"], o["isAdmin"] = rev, adm
        if ex and ex.id == actor.id and not adm: res.append(fail("You can't remove your own Admin role through an import")); continue
        if r.get("region") is not None and str(r.get("region")).strip():
            try: o["regions"] = regions_from(c["regions"], r["region"])
            except Err as e: res.append(fail(e.msg)); continue
        elif ex: o["regions"] = ex.regions
        if not o["regions"] and not adm: res.append(fail("Region is required for non-admin members")); continue
        if r.get("eligibility") is not None:
            toks = [t.strip() for t in re.split(r"[,;|]+", str(r["eligibility"])) if t.strip()]
            if any(norm_region(c["regions"], t) for t in toks): res.append(fail("Put regions in the Region column; Eligibility is for profile types (Full, Limited...)")); continue
            try: o["profileTypes"] = ", ".join(_types_from(c, r["eligibility"]))
            except Err as e: res.append(fail(e.msg)); continue
        elif ex: o["profileTypes"] = ex.profile_types
        o["action"] = "update" if ex else "add"
        o["message"] = ("Existing member will be updated" + ("" if ex.active else " (currently inactive, stays inactive)")) if ex else "New member will be added"
        res.append(o)
    return res

def previewTeamImport(db, u, p):
    need(is_admin(u)); rows = p.get("rows", [])
    if len(rows) > 500: raise Err("Too many rows (max 500 per upload)")
    return _eval_team(db, u, rows)

def importTeam(db, u, p):
    need(is_admin(u)); rows = p.get("rows", [])
    if len(rows) > 500: raise Err("Too many rows (max 500 per upload)")
    res = _eval_team(db, u, rows); added = updated = 0
    for r, src in zip(res, rows):
        if r["action"] == "error": continue
        x = db.scalar(select(User).where(User.email == r["email"]))
        if x is None: x = User(email=r["email"], active=True); db.add(x); added += 1; verb = "Added Member (CSV)"
        else: updated += 1; verb = "Updated Member (CSV)"
        x.name, x.title, x.is_reviewer, x.is_admin = r["name"], r["title"], r["isReviewer"], r["isAdmin"]
        x.set_regions(r["regions"])
        if src.get("eligibility") is not None or verb.startswith("Added"): x.profile_types = r["profileTypes"]
        log(db, u, verb, r["email"], f"{r['name']} / {r['title']}")
    errs = [r for r in res if r["action"] == "error"]
    log(db, u, "Team CSV Import", "", f"{added} added, {updated} updated, {len(errs)} skipped")
    return {"added": added, "updated": updated, "errors": errs}

def saveConfig(db, u, p):
    need(is_admin(u)); c = cfg(db)
    num = lambda k, d: int(p[k]) if p.get(k) not in (None, "") else d
    if p.get("regions") is not None:
        nxt = []
        for raw in p["regions"]:
            t = " ".join(str(raw or "").split())
            if not t: continue
            if not re.fullmatch(r"[A-Za-z0-9 +&/._-]{1,24}", t): raise Err(f'Invalid region "{raw}". Use up to 24 letters, numbers, spaces or + & / . _ -')
            if t.lower() not in [x.lower() for x in nxt]: nxt.append(t)
        if not nxt: raise Err("At least one region is required.")
        for r in c["regions"]:
            if r.lower() in [x.lower() for x in nxt]: continue
            open_n = cnt(db, Asset, Asset.region == r, Asset.status != "Reviewed"); ppl = cnt(db, UserRegion, UserRegion.region == r)
            if open_n or ppl: raise Err(f"Cannot remove {r}: used by {open_n} open asset(s) and {ppl} team member(s). Move or update them first.")
        set_cfg(db, "regions", nxt)
    if "cmsUrlTemplate" in p:
        t = str(p["cmsUrlTemplate"] or "").strip()
        if t and not (http_url(t.replace("{asset_id}", "x")) and "{asset_id}" in t): raise Err("CMS link template must start with https:// and contain {asset_id}")
        set_cfg(db, "cmsUrlTemplate", t)
    if p.get("output") is not None: set_cfg(db, "output", _clean_output(p["output"]))
    set_cfg(db, "maxPipeline", num("maxPipeline", 5)); set_cfg(db, "overdueHours", num("overdueHours", c["overdueHours"]))
    set_cfg(db, "reviewSlaHours", num("reviewSlaHours", c["reviewSlaHours"]))
    set_cfg(db, "weekendLock", "true" if p.get("weekendLock", c["weekendLock"]) else "false")
    set_cfg(db, "ownershipOrder", p.get("ownershipOrder") or c["ownershipOrder"]); set_cfg(db, "qa", p.get("qa") or c["qa"])
    log(db, u, "Changed Settings", "", json.dumps(p)[:500]); return 1

def audit(db, u, p):
    need(is_admin(u))
    return [{"time": ts(a.ts), "user": a.user_name, "action": a.action, "asset": a.asset_ref, "detail": a.detail}
            for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(300))]

# ---------------- v2: KPI, quality, overview, projects, leave, notifications ----------------
def period_start(period):
    t = now()
    return datetime(t.year, t.month, 1) if period == "month" else datetime(t.year, t.month, t.day) - timedelta(days=t.weekday())

def kpi(db, u, p):   # admin sees the team; everyone else only their own row
    s0 = period_start(p.get("period")); rows = []
    for x in visible_users(db, u):
        rows.append({"name": x.name, "role": x.role,
                     "updated": cnt(db, Asset, Asset.assignee_id == x.id, Asset.completed_at >= s0),
                     "reviewed": cnt(db, Asset, Asset.reviewer_id == x.id, Asset.review_status == "Completed", Asset.review_date >= s0),
                     "projects": cnt(db, Project, Project.assignee_id == x.id, Project.status == "Completed", Project.completed_at >= s0),
                     "active": active_count(db, x.id), "max": limit_for(db, x)})
    return {"rows": rows, "since": ts(s0), "totals": {"updated": sum(r["updated"] for r in rows), "reviewed": sum(r["reviewed"] for r in rows),
            "active": sum(r["active"] for r in rows), "available": cnt(db, Asset, Asset.status == "Available")}}

def quality(db, u, p):
    s0 = period_start(p.get("period")); rows = []
    for x in visible_users(db, u):
        a = db.scalars(select(Asset).where(Asset.assignee_id == x.id, Asset.review_status == "Completed", Asset.review_date >= s0)).all()
        avg = lambda k: round(sum(getattr(i, k) or 0 for i in a) / len(a)) if a else None
        rows.append({"name": x.name, "reviews": len(a), "qa": avg("qa_pct"), "difficulty": avg("difficulty_pct")})
    q = [r["qa"] for r in rows if r["qa"] is not None]
    return {"rows": rows, "teamAvg": round(sum(q) / len(q)) if q else None, "reviews": sum(r["reviews"] for r in rows)}

def overview(db, u, p):   # manager dashboard (admin only): workload, output, QA, bottlenecks, trend
    need(is_admin(u)); c = cfg(db); t = now(); today = datetime(t.year, t.month, t.day)
    k, q = kpi(db, u, p), quality(db, u, p)
    start = today - timedelta(days=13)
    upd = [x.completed_at.date() for x in db.scalars(select(Asset).where(Asset.completed_at >= start))]
    rev = [x.review_date.date() for x in db.scalars(select(Asset).where(Asset.review_date >= start))]
    days = [(start + timedelta(days=i)).date() for i in range(14)]
    analysts = [r for r in k["rows"] if r["role"] != "Admin" or r["active"]]
    out_today = {x.assignee_id for x in db.scalars(select(Asset).where(Asset.completed_at >= today))}
    names = {x.id: x.name for x in db.scalars(select(User).where(User.active))}
    return {"workload": [{"name": r["name"], "active": r["active"], "max": r["max"]} for r in analysts],
            "output": [{"name": r["name"], "updated": r["updated"], "reviewed": r["reviewed"]} for r in k["rows"]],
            "qa": [{"name": r["name"], "qa": r["qa"]} for r in q["rows"]], "teamAvg": q["teamAvg"],
            "trend": [{"day": d.isoformat(), "updated": upd.count(d), "reviewed": rev.count(d)} for d in days],
            "bottlenecks": {"assignedOver": cnt(db, Asset, Asset.status.in_(ACTIVE), Asset.assigned_at < t - timedelta(hours=c["overdueHours"])),
                            "reviewOver": cnt(db, Asset, Asset.status == "Pending Review", Asset.completed_at < t - timedelta(hours=c["reviewSlaHours"])),
                            "pendingReview": cnt(db, Asset, Asset.status == "Pending Review"), "available": k["totals"]["available"],
                            "reviewedToday": cnt(db, Asset, Asset.review_date >= today), "overdueHours": c["overdueHours"], "reviewSlaHours": c["reviewSlaHours"]},
            "noOutputToday": sorted(n for i, n in names.items() if i not in out_today and any(r["name"] == n and r["role"] != "Admin" for r in k["rows"]))}

def pout(x):
    return {"pid": x.id, "id": x.ref, "name": x.name, "project": x.project, "priority": x.priority, "requestDate": x.request_date.isoformat() if x.request_date else "",
            "status": x.status, "assignee": x.assignee.name if x.assignee else "", "assignedAt": ts(x.assigned_at), "completedAt": ts(x.completed_at)}

def projectList(db, u, p):
    if is_admin(u):
        items = db.scalars(select(Project).order_by(Project.status, Project.request_date.is_(None), Project.request_date, Project.id).limit(400)).all()
        members = [x.name for x in db.scalars(select(User).where(User.active).order_by(User.name))]
    else:
        items = db.scalars(select(Project).where(Project.assignee_id == u.id).order_by(Project.status, Project.id)).all(); members = []
    c = {s: cnt(db, Project, Project.status == s) for s in ("Available", "Assigned", "In Progress", "Completed")} if is_admin(u) else \
        {s: cnt(db, Project, Project.status == s, Project.assignee_id == u.id) for s in ("Available", "Assigned", "In Progress", "Completed")}
    return {"items": [pout(x) for x in items], "counts": c, "members": members}

def projectImport(db, u, p):
    need(is_admin(u)); have = {(x.project, x.ref) for x in db.execute(select(Project.project, Project.ref)).all()}; added = dup = 0
    for r in p.get("rows", [])[:20000]:
        ref, proj = str(r.get("id", "")).strip(), (r.get("project") or "General").strip()
        if not ref: continue
        if (proj, ref) in have: dup += 1; continue
        have.add((proj, ref)); added += 1
        db.add(Project(ref=ref, name=r.get("name", ""), project=proj, priority=r.get("priority") or "Normal", request_date=pdate(r.get("requestDate"))))
    log(db, u, "Project CSV Import", "", f"{added} new, {dup} duplicates"); return {"added": added, "dup": dup}

def _assign_projects(db, u, items, to):
    t = db.scalar(select(User).where(User.name == to, User.active))
    if not t: raise Err("User not found")
    for x in items: x.assignee_id, x.status, x.assigned_at = t.id, "Assigned", now()
    if items:
        notify(db, t.id, "project", f"{len(items)} project item(s) assigned to you", "project")
        log(db, u, "Project Assigned", "", f"{len(items)} -> {t.name}")
    return len(items)

def projectAssign(db, u, p):
    need(is_admin(u)); ids = [int(i) for i in p.get("pids", [])]
    items = db.scalars(select(Project).where(Project.id.in_(ids), Project.status == "Available")).all()
    return _assign_projects(db, u, items, p.get("to"))

def projectAllocate(db, u, p):   # bulk: oldest N available (optionally one project) -> a person
    need(is_admin(u)); n = max(1, min(int(p.get("count") or 1), 500))
    st = select(Project).where(Project.status == "Available")
    if p.get("project"): st = st.where(Project.project == p["project"])
    return _assign_projects(db, u, db.scalars(st.order_by(Project.request_date.is_(None), Project.request_date, Project.id).limit(n)).all(), p.get("to"))

def _own_project(db, u, pid):
    x = db.get(Project, int(pid))
    if not x or x.assignee_id != u.id: raise Err("Not your project item", 403)
    return x
def projectStart(db, u, p):
    x = _own_project(db, u, p["pid"]); x.status = "In Progress"; x.started_at = x.started_at or now(); return 1
def projectComplete(db, u, p):
    x = _own_project(db, u, p["pid"]); x.status, x.completed_at = "Completed", now(); log(db, u, "Project Completed", x.ref, x.name); return 1

def leaveView(db, u, p):
    t = now().date(); ws = pdate(p.get("week")) or t; ws -= timedelta(days=ws.weekday())
    days = [ws + timedelta(days=i) for i in range(7)]
    qs = date(t.year, 3 * ((t.month - 1) // 3) + 1, 1); nm = qs.month + 3
    qe = date(qs.year + (nm > 12), (nm - 1) % 12 + 1, 1) - timedelta(days=1)
    users = visible_users(db, u); by = {x.id: x for x in users}
    ls = db.scalars(select(Leave).where(Leave.user_id.in_(list(by)), Leave.day >= min(ws, qs), Leave.day <= max(days[-1], qe))).all()
    wk = [l for l in ls if ws <= l.day <= days[-1]]; qd = {}
    for l in ls:
        if qs <= l.day <= qe: qd[by[l.user_id].name] = qd.get(by[l.user_id].name, 0) + 1
    return {"days": [d.isoformat() for d in days], "week": ws.isoformat(), "users": [{"name": x.name, "role": x.role} for x in users],
            "leaves": [{"id": l.id, "user": by[l.user_id].name, "date": l.day.isoformat(), "type": l.type, "note": l.note} for l in wk],
            "quarter": {"start": qs.isoformat(), "end": qe.isoformat(), "byUser": qd, "total": sum(qd.values())},
            "capacity": len(users) * 5 - sum(1 for l in wk if l.day.weekday() < 5)}

def leaveAdd(db, u, p):
    need(is_admin(u)); x = db.scalar(select(User).where(User.name == p.get("user")))
    d1 = pdate(p.get("from")); d2 = pdate(p.get("to")) or d1
    if not x or not d1 or d2 < d1: raise Err("Employee and a valid date (range) are required")
    if (d2 - d1).days > 60: raise Err("Maximum 61 days at a time")
    n, d = 0, d1
    while d <= d2:
        if (d1 == d2 or d.weekday() < 5) and not db.scalar(select(Leave.id).where(Leave.user_id == x.id, Leave.day == d)):
            db.add(Leave(user_id=x.id, day=d, type=p.get("type") or "Leave", note=str(p.get("note", ""))[:200])); n += 1
        d += timedelta(days=1)
    log(db, u, "Leave Added", x.email, f"{p.get('type')} {d1}..{d2} ({n} days)"); return {"added": n}

def leaveDelete(db, u, p):
    need(is_admin(u)); l = db.get(Leave, int(p["id"]))
    if not l: raise Err("Not found", 404)
    db.delete(l); log(db, u, "Leave Removed", "", str(l.day)); return 1

def notifications(db, u, p):
    if is_admin(u): _gap_alerts(db, u)
    cut = now() - timedelta(hours=cfg(db)["overdueHours"])
    for a in db.scalars(select(Asset).where(Asset.assignee_id == u.id, Asset.status.in_(ACTIVE), Asset.assigned_at < cut)):
        ref = f"{a.asset_id}:{ts(a.assigned_at)}"
        if not db.scalar(select(Notification.id).where(Notification.user_id == u.id, Notification.kind == "overdue", Notification.ref == ref)):
            notify(db, u.id, "overdue", f"{a.name} has been pending for over {cfg(db)['overdueHours']} hours", ref)
    db.flush(); unread = cnt(db, Notification, Notification.user_id == u.id, Notification.read.is_(False))
    if p.get("countOnly"): return {"unread": unread}
    items = db.scalars(select(Notification).where(Notification.user_id == u.id).order_by(Notification.id.desc()).limit(50)).all()
    return {"unread": unread, "items": [{"id": n.id, "time": ts(n.ts), "kind": n.kind, "message": n.message, "read": n.read} for n in items]}

def markRead(db, u, p):
    st = select(Notification).where(Notification.user_id == u.id, Notification.read.is_(False))
    if p.get("ids"): st = st.where(Notification.id.in_([int(i) for i in p["ids"]]))
    for n in db.scalars(st): n.read = True
    return 1

# ================= v3: gaps, who is on, weighted stats, search, exports =================
DEF_OUTPUT = {"profileType": {"Full": 1.0, "Limited": 0.75, "Minimal": 0.5},   # PLACEHOLDER weights: admins edit these in Settings
              "bands": [{"upTo": 33, "bonus": 0.0}, {"upTo": 66, "bonus": 0.25}, {"upTo": 100, "bonus": 0.5}], "reviewWeight": 0.4}
AVAIL = ("Available", "Not available", "On leave")

def _clean_output(o):
    try:
        pt = {str(k).strip(): float(v) for k, v in (o.get("profileType") or {}).items() if str(k).strip()}
        bands = sorted(({"upTo": int(b["upTo"]), "bonus": float(b["bonus"])} for b in o.get("bands") or []), key=lambda b: b["upTo"])
        rw = float(o.get("reviewWeight", 0.4))
    except (TypeError, ValueError, KeyError, AttributeError): raise Err("Output weights are not valid numbers")
    if not pt or any(not 0 <= v <= 10 for v in pt.values()): raise Err("Profile-type weights must be between 0 and 10")
    if not bands or bands[-1]["upTo"] != 100 or any(not 1 <= b["upTo"] <= 100 or not 0 <= b["bonus"] <= 5 for b in bands): raise Err("Complexity bands must end at 100 and have bonuses between 0 and 5")
    if len({b["upTo"] for b in bands}) != len(bands): raise Err("Complexity bands must have different upper limits")
    if not 0 <= rw <= 5: raise Err("Review weight must be between 0 and 5")
    return {"profileType": pt, "bands": bands, "reviewWeight": rw}

def pct_for(a): return a.difficulty_pct if a.difficulty_pct is not None else a.upd_difficulty_pct   # the reviewer's reading wins once it exists

def points(a, oc):
    base = oc["profileType"].get(a.profile_type or "", 1.0); pct = pct_for(a); bonus = 0.0
    if pct is not None: bonus = next((b["bonus"] for b in oc["bands"] if pct <= b["upTo"]), oc["bands"][-1]["bonus"])
    return base * (1 + bonus)

def leave_ids_today(db): return set(db.scalars(select(Leave.user_id).where(Leave.day == now().date())))
def eff_status(x, leaves): return "On leave" if x.id in leaves else (x.availability or "Available")
def eligible_for(x, ptype):
    t = csv_list(x.profile_types); return not t or (ptype or "").lower() in t
def open_counts(db): return dict(db.execute(select(Asset.assignee_id, func.count()).where(Asset.status.in_(ACTIVE)).group_by(Asset.assignee_id)).all())

def _gap_groups(db):
    c = cfg(db); leaves = leave_ids_today(db); opens = open_counts(db)
    people = [x for x in db.scalars(select(User).where(User.active)) if x.regions]
    res = []
    for region, ptype, n in db.execute(select(Asset.region, Asset.profile_type, func.count()).where(Asset.status == "Available").group_by(Asset.region, Asset.profile_type)).all():
        holders = [x for x in people if region in x.regions]
        if region not in c["regions"]: kind, why, fix = "gap", f'Region "{region}" is not in the allowed list', "Fix the region on these assets or add it in Settings"
        elif not holders: kind, why, fix = "gap", f"Nobody is assigned to {region}", f"Add {region} to someone's regions on the Team page"
        else:
            el = [x for x in holders if eligible_for(x, ptype)]
            if not el: kind, why, fix = "gap", f"Nobody in {region} is eligible for {ptype or 'these'} profiles", "Widen someone's profile-type eligibility or add a person"
            else:
                av = [x for x in el if eff_status(x, leaves) == "Available"]
                if not av: kind, why, fix = "away", f"Everyone eligible in {region} is away today", "Wait for them to return, or add cover"
                elif not [x for x in av if opens.get(x.id, 0) < (x.max_pipeline or c["maxPipeline"])]: kind, why, fix = "capacity", f"Everyone available in {region} is at their pipeline limit", "Normal: these are handed out as slots free up"
                else: continue
        res.append({"kind": kind, "region": region, "profileType": ptype, "count": n, "reason": why, "fix": fix, "people": [x.name for x in holders]})
    return res

def _gap_alerts(db, u):
    day = now().date().isoformat()
    for g in _gap_groups(db):
        if g["kind"] == "capacity": continue
        ref = f"gap:{day}:{g['kind']}:{g['region']}:{g['profileType']}"[:100]
        if not db.scalar(select(Notification.id).where(Notification.user_id == u.id, Notification.ref == ref)):
            notify(db, u.id, "gap", f"{g['count']} asset(s) nobody can take: {g['reason']}. Fix: {g['fix']}.", ref)
    db.flush()

def unassignable(db, u, p):
    need(is_admin(u)); tpl = cfg(db)["cmsUrlTemplate"]; groups = sorted(_gap_groups(db), key=lambda g: ({"gap": 0, "away": 1, "capacity": 2}[g["kind"]], -g["count"]))
    stuck = []
    for g in groups:
        if g["kind"] in ("gap", "away") and len(stuck) < 40:
            stuck += db.scalars(select(Asset).where(Asset.status == "Available", Asset.region == g["region"], Asset.profile_type == g["profileType"]).order_by(Asset.request_date).limit(10)).all()
    tot = lambda k: sum(g["count"] for g in groups if g["kind"] == k)
    return {"groups": groups, "assets": [{**out(a, tpl), "stuckReason": next(g["reason"] for g in groups if g["region"] == a.region and g["profileType"] == a.profile_type) } for a in stuck[:40]],
            "totals": {"gap": tot("gap"), "away": tot("away"), "capacity": tot("capacity")}}

def setAvailability(db, u, p):
    st = p.get("status")
    if st not in AVAIL: raise Err("Status must be Available, Not available or On leave")
    t = u
    if p.get("user") and p["user"] != u.name:
        need(is_admin(u)); t = db.scalar(select(User).where(User.name == p["user"], User.active))
        if not t: raise Err("User not found")
    if st == "On leave" and not is_admin(u): raise Err("Only an admin can mark someone On leave", 403)
    if t.id == u.id and t.availability == "On leave" and not is_admin(u): raise Err("An admin set you On leave. Ask them to set you back to Available.", 403)
    t.availability, t.availability_note, t.availability_by, t.availability_at = st, str(p.get("note") or "")[:200], u.name, now()
    log(db, u, "Availability", t.email, f"{t.name}: {st}" + (f" ({t.availability_note})" if t.availability_note else "")); return 1

def whoIsOn(db, u, p):
    need(is_admin(u)); c = cfg(db); leaves = leave_ids_today(db); opens = open_counts(db); rows = []; byreg = {r: {"region": r, "people": 0, "free": 0} for r in c["regions"]}
    for x in db.scalars(select(User).where(User.active).order_by(User.name)):
        st = eff_status(x, leaves); mx = x.max_pipeline or c["maxPipeline"]; op = opens.get(x.id, 0)
        cap = "not taking work" if st != "Available" else "full" if op >= mx else f"{mx - op} free"
        if st == "Available" and op < mx:
            for r in x.regions:
                if r in byreg: byreg[r]["people"] += 1; byreg[r]["free"] += mx - op
        rows.append({"name": x.name, "title": x.title, "isReviewer": bool(x.is_reviewer), "isAdmin": bool(x.is_admin), "regions": x.regions, "status": st,
                     "fromCalendar": x.id in leaves, "note": x.availability_note if st == x.availability else "Leave calendar", "setBy": x.availability_by if st == x.availability else "",
                     "open": op, "max": mx, "capacity": cap})
    cnt_ = lambda s_: sum(1 for r in rows if r["status"] == s_)
    return {"rows": rows, "summary": {"available": cnt_("Available"), "notAvailable": cnt_("Not available"), "onLeave": cnt_("On leave")}, "byRegion": list(byreg.values())}

def teamStats(db, u, p):
    today = now().date(); to_d = pdate(p.get("to")) or today; from_d = pdate(p.get("from")) or (to_d - timedelta(days=29))
    if from_d > to_d: raise Err("'From' must be on or before 'To'")
    a0, a1 = datetime(from_d.year, from_d.month, from_d.day), datetime(to_d.year, to_d.month, to_d.day) + timedelta(days=1)
    oc = cfg(db)["output"]; rows = []
    for x in visible_users(db, u):
        done = db.scalars(select(Asset).where(Asset.assignee_id == x.id, Asset.completed_at >= a0, Asset.completed_at < a1)).all()
        revs = db.scalars(select(Asset).where(Asset.reviewer_id == x.id, Asset.review_status == "Completed", Asset.review_date >= a0, Asset.review_date < a1)).all()
        diffs = [pct_for(a) for a in done if pct_for(a) is not None]; qas = [a.qa_pct for a in done if a.qa_pct is not None]
        cmp_ = [a for a in done if a.upd_difficulty_pct is not None and a.difficulty_pct is not None]
        differ = [a for a in cmp_ if (a.upd_own_diff, a.upd_type_diff, a.upd_tx_diff) != (a.own_diff, a.type_diff, a.tx_diff)]
        rows.append({"name": x.name, "title": x.title, "updates": len(done), "weighted": round(sum(points(a, oc) for a in done), 1), "reviews": len(revs),
                     "reviewPoints": round(len(revs) * oc["reviewWeight"], 1), "avgDifficulty": round(sum(diffs) / len(diffs)) if diffs else None,
                     "avgQa": round(sum(qas) / len(qas)) if qas else None, "byType": {t: sum(1 for a in done if (a.profile_type or "") == t) for t in PROFILE_TYPES},
                     "compared": len(cmp_), "differ": len(differ), "avgGap": round(sum(abs(a.upd_difficulty_pct - a.difficulty_pct) for a in cmp_) / len(cmp_)) if cmp_ else None})
    rows.sort(key=lambda r: -(r["weighted"] + r["reviewPoints"]))
    return {"from": from_d.isoformat(), "to": to_d.isoformat(), "rows": rows, "output": oc,
            "totals": {"updates": sum(r["updates"] for r in rows), "weighted": round(sum(r["weighted"] for r in rows), 1), "reviews": sum(r["reviews"] for r in rows)}}

def _date_filter(st, p, prefix=""):
    field = {"requestDate": Asset.request_date, "completedAt": Asset.completed_at, "reviewDate": Asset.review_date}.get(p.get("dateField"), Asset.request_date)
    d1, d2 = pdate(p.get("dateFrom")), pdate(p.get("dateTo"))
    is_dt = field is not Asset.request_date
    if d1: st = st.where(field >= (datetime(d1.year, d1.month, d1.day) if is_dt else d1))
    if d2: st = st.where(field < datetime(d2.year, d2.month, d2.day) + timedelta(days=1)) if is_dt else st.where(field <= d2)
    return st

def search(db, u, p):
    """Search the whole history. Admins: every region. Everyone else: only their own regions (the region wall still applies)."""
    adm = is_admin(u); tpl = cfg(db)["cmsUrlTemplate"]; regs = scope(db, u, p.get("region"))
    st = select(Asset).where(Asset.region.in_(regs))
    q = " ".join(str(p.get("q") or "").split()).lower()
    if q:
        like = f"%{q}%"; conds = [func.lower(Asset.asset_id).like(like), func.lower(Asset.name).like(like)]
        if adm: conds += [func.lower(Asset.comments).like(like), func.lower(Asset.cms_notes).like(like), Asset.assignee.has(func.lower(User.name).like(like)), Asset.reviewer.has(func.lower(User.name).like(like))]
        st = st.where(or_(*conds))
    for key, col in (("status", Asset.status), ("ownership", Asset.ownership), ("profileType", Asset.profile_type)):
        if p.get(key): st = st.where(col == p[key])
    if p.get("person"):
        if not adm and p["person"] != u.name: raise Err("You can only filter by your own name", 403)
        st = st.where(or_(Asset.assignee.has(User.name == p["person"]), Asset.reviewer.has(User.name == p["person"])))
    st = _date_filter(st, p)
    total = db.scalar(select(func.count()).select_from(st.subquery()))
    by_status = dict(db.execute(st.with_only_columns(Asset.status, func.count()).group_by(Asset.status)).all())
    by_region = dict(db.execute(st.with_only_columns(Asset.region, func.count()).group_by(Asset.region)).all())
    off = max(0, int(p.get("offset") or 0)); items = []
    for a in db.scalars(st.order_by(Asset.id.desc()).offset(off).limit(50)):
        own = adm or a.assignee_id == u.id
        items.append({"id": a.asset_id, "name": a.name, "region": a.region, "ownership": a.ownership, "profileType": a.profile_type, "status": a.status, "requestDate": a.request_date.isoformat() if a.request_date else "",
                      "assignee": a.assignee.name if a.assignee else "", "reviewer": a.reviewer.name if (a.reviewer and (adm or own)) else "", "completedAt": ts(a.completed_at), "reviewDate": ts(a.review_date),
                      "percentage": a.qa_pct if own else "", "cmsUrl": cms_link(a, tpl)})
    return {"total": total, "offset": off, "items": items, "byStatus": by_status, "byRegion": by_region}

EXPORT_COLS = [("id", "Asset ID", "Asset", False), ("name", "Asset", "Asset", False), ("region", "Region", "Asset", False), ("profileType", "Profile type", "Asset", False), ("ownership", "Ownership", "Asset", False),
    ("updateReason", "Update reason", "Asset", False), ("requestDate", "Request update from", "Asset", False), ("status", "Status", "Asset", False), ("cmsUrl", "CMS link", "Asset", False),
    ("assignedAt", "Assigned at", "Timing", False), ("completedAt", "Updated at", "Timing", False), ("reviewDate", "Reviewed at", "Timing", False),
    ("assignee", "Updated by", "People", False), ("reviewer", "Reviewed by", "People", False),
    ("qa", "QA %", "Internal", True), ("difficulty", "Reviewer difficulty %", "Internal", True), ("updDifficulty", "Updater difficulty %", "Internal", True),
    ("points", "Weighted points", "Internal", True), ("minutes", "Time spent (min)", "Internal", True), ("comments", "Reviewer comments", "Internal", True)]
DEFAULT_COLS = [k for k, _, _, internal in EXPORT_COLS if not internal and k in ("id", "name", "region", "profileType", "ownership", "requestDate", "status", "completedAt")]

def _cell(k, a, tpl, oc):
    g = {"id": a.asset_id, "name": a.name, "region": a.region, "profileType": a.profile_type, "ownership": a.ownership, "updateReason": a.update_reason, "status": a.status,
         "requestDate": a.request_date.isoformat() if a.request_date else "", "cmsUrl": cms_link(a, tpl), "assignedAt": ts(a.assigned_at), "completedAt": ts(a.completed_at), "reviewDate": ts(a.review_date),
         "assignee": a.assignee.name if a.assignee else "", "reviewer": a.reviewer.name if a.reviewer else "", "qa": a.qa_pct if a.qa_pct is not None else "",
         "difficulty": a.difficulty_pct if a.difficulty_pct is not None else "", "updDifficulty": a.upd_difficulty_pct if a.upd_difficulty_pct is not None else "",
         "points": round(points(a, oc), 2) if a.completed_at else "", "minutes": round((a.completed_at - a.assigned_at).total_seconds() / 60) if a.completed_at and a.assigned_at else "", "comments": a.comments}
    return g[k]

def exportMeta(db, u, p):
    need(is_admin(u)); c = cfg(db)
    return {"columns": [{"key": k, "label": l, "group": g, "internal": i} for k, l, g, i in EXPORT_COLS], "defaults": DEFAULT_COLS, "regions": c["regions"],
            "statuses": ["Available", "Assigned", "In Progress", "Pending Review", "Reviewed"], "ownership": c["ownershipOrder"], "profileTypes": list(dict.fromkeys(PROFILE_TYPES + list(c["qa"]["typeScore"])))}

def _export_query(db, f):
    st = select(Asset)
    for key, col in (("regions", Asset.region), ("statuses", Asset.status), ("ownership", Asset.ownership), ("profileTypes", Asset.profile_type)):
        if f.get(key): st = st.where(col.in_(list(f[key])))
    return _date_filter(st, f)

def exportRun(db, u, p):
    need(is_admin(u)); c = cfg(db); prof = None
    if p.get("profileId"):
        prof = db.get(ExportProfile, int(p["profileId"]))
        if not prof: raise Err("Saved profile not found", 404)
        f, cols, fmt, tabs = json.loads(prof.filters), json.loads(prof.columns), prof.fmt, prof.tab_per_region
    else: f, cols, fmt, tabs = p.get("filters") or {}, p.get("columns") or DEFAULT_COLS, p.get("format", "csv"), bool(p.get("tabPerRegion"))
    valid = {k: l for k, l, _, _ in EXPORT_COLS}; cols = [k for k in cols if k in valid]
    if not cols: raise Err("Pick at least one column")
    if fmt not in ("csv", "xlsx"): raise Err("Format must be csv or xlsx")
    assets = db.scalars(_export_query(db, f).order_by(Asset.region, Asset.id).limit(50001)).all()
    if len(assets) > 50000: raise Err("Too many rows (max 50,000). Narrow the filters.")
    rows = [[_cell(k, a, c["cmsUrlTemplate"], c["output"]) for k in cols] for a in assets]; head = [valid[k] for k in cols]
    stamp = now().strftime("%Y%m%d-%H%M"); base = re.sub(r"[^A-Za-z0-9_-]+", "_", prof.name if prof else "export")
    if fmt == "csv":
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(head); w.writerows(rows); data = buf.getvalue().encode("utf-8-sig"); mime = "text/csv"
    else:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook(); wb.remove(wb.active)
        groups = {}
        for a, r in zip(assets, rows): groups.setdefault(a.region if tabs else "Export", []).append(r)
        for name, rr in (groups or {"Export": []}).items():
            ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", "-", name)[:31] or "Export"); ws.append(head)
            for cell in ws[1]: cell.font = Font(bold=True)
            for r in rr: ws.append(r)
        bio = io.BytesIO(); wb.save(bio); data = bio.getvalue(); mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    db.add(ExportLog(ts=now(), user_name=u.name, profile_name=prof.name if prof else "", filters=json.dumps(f), rows=len(rows), fmt=fmt))
    log(db, u, "Export", "", f"{len(rows)} rows, {fmt}{' via ' + prof.name if prof else ''}")
    return {"filename": f"{base}-{stamp}.{fmt}", "mime": mime, "rows": len(rows), "contentBase64": base64.b64encode(data).decode()}

def exportProfiles(db, u, p):
    need(is_admin(u))
    return [{"id": x.id, "name": x.name, "filters": json.loads(x.filters), "columns": json.loads(x.columns), "format": x.fmt, "tabPerRegion": x.tab_per_region, "createdBy": x.created_by, "createdAt": ts(x.created_at)}
            for x in db.scalars(select(ExportProfile).order_by(ExportProfile.name))]

def exportSaveProfile(db, u, p):
    need(is_admin(u)); name = " ".join(str(p.get("name") or "").split())
    if not name: raise Err("Give the profile a name")
    cols = [k for k in (p.get("columns") or []) if k in {c[0] for c in EXPORT_COLS}]
    if not cols: raise Err("Pick at least one column")
    x = db.get(ExportProfile, int(p["id"])) if p.get("id") else db.scalar(select(ExportProfile).where(ExportProfile.name == name))
    if x is None: x = ExportProfile(created_by=u.name, created_at=now()); db.add(x)
    x.name, x.filters, x.columns, x.fmt, x.tab_per_region = name, json.dumps(p.get("filters") or {}), json.dumps(cols), p.get("format", "csv"), bool(p.get("tabPerRegion"))
    log(db, u, "Saved Export Profile", "", name); return 1

def exportDeleteProfile(db, u, p):
    need(is_admin(u)); x = db.get(ExportProfile, int(p["id"]))
    if not x: raise Err("Not found", 404)
    log(db, u, "Deleted Export Profile", "", x.name); db.delete(x); return 1

def exportLog(db, u, p):
    need(is_admin(u))
    return [{"time": ts(x.ts), "user": x.user_name, "profile": x.profile_name or "Ad hoc", "filters": json.loads(x.filters), "rows": x.rows, "format": x.fmt}
            for x in db.scalars(select(ExportLog).order_by(ExportLog.id.desc()).limit(100))]

HANDLERS = {f.__name__: f for f in (boot, dashboard, getNext, start, complete, reviewQueue, pickReview, releaseReview, assignReview, submitReview, pipeline, team, addAsset, importCsv,
            reassign, saveUser, previewTeamImport, importTeam, saveConfig, audit, kpi, quality, overview, projectList, projectImport, projectAssign, projectAllocate,
            projectStart, projectComplete, leaveView, leaveAdd, leaveDelete, notifications, markRead,
            unassignable, setAvailability, whoIsOn, teamStats, search, exportMeta, exportRun, exportProfiles, exportSaveProfile, exportDeleteProfile, exportLog)}
