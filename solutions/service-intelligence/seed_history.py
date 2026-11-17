#!/usr/bin/env python3
"""Service Intelligence — ~90 days of FICTIONAL application history for the City Development Authority demo, so the
department head's dashboards and AI analysis have something to work with. Deterministic (seeded). Every record is
marked: case/task scope `_synthetic: true`, titles start with "[demo]", ids contain "-SYN". No Inbox items are written
(officers' inboxes stay clean). Runs INSIDE the staging app-host as the department's fulfilment principal.
  MODE=apply (default) writes · MODE=dry prints what it would write.
Built-in patterns (for the hotspots / AI to find): zone-4 scrutiny slow (~4.5 d vs a 3 d SLA); an approval backlog in
the last three weeks; ~25% of freehold applications returned at scrutiny, mostly for a missing no-dues certificate;
name-transfer volume rising; ~5% rejections."""
import json, os, random, sys, datetime as dt, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
IDN, K, T = "http://identity:8093", "http://kernel:8080", "city-da"
MODE = os.environ.get("MODE", "apply")
NOW = dt.datetime.utcnow().replace(microsecond=0)
R = random.Random("service-intelligence-demo")
ZONES = ["zone-1", "zone-2", "zone-3", "zone-4"]
SVC = {"cda-name-transfer": {"title": "Name transfer (mutation) of a property", "fee": 2500, "base": 2.2, "growth": 0.035, "returns": 0.07},
       "city-da-freehold-conversion": {"title": "Freehold conversion", "fee": None, "base": 1.1, "growth": 0.0, "returns": 0.25}}
SLA = {"scrutiny": 72, "approval": 48}
RETURN_REASONS = {"city-da-freehold-conversion": [("no-dues certificate missing", 6), ("lease deed not attached", 2), ("reason for conversion unclear", 1)],
                  "cda-name-transfer": [("sale deed not attached", 3), ("present holder name does not match the record", 2), ("no-dues certificate missing", 1)]}
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
def wpick(pairs):
    tot = sum(w for _, w in pairs); x = R.uniform(0, tot)
    for v, w in pairs:
        x -= w
        if x <= 0: return v
    return pairs[-1][0]
def hours(median_h, spread=0.55):
    return max(0.5, R.lognormvariate(0, spread) * median_h)

recs = []
n = 0
for day in range(90, 0, -1):
    for sid, cfg in SVC.items():
        lam = cfg["base"] * (1 + cfg["growth"]) ** ((90 - day) / 7.0)
        k = sum(1 for _ in range(int(lam * 3)) if R.random() < 1 / 3)            # ~Poisson(lam)
        for _ in range(k):
            n += 1
            zone = R.choices(ZONES, [3, 3, 2, 3])[0]
            opened = NOW - dt.timedelta(days=day) + dt.timedelta(hours=R.uniform(9, 17))
            tag = "SYN%05d" % n
            cid = "ds:%s:case/%s-%s" % (T, sid, tag)
            area = "ds:%s:area/%s" % (T, zone)
            scope = {"area": area, "area_path": [area, "ds:%s:area/city" % T], "_synthetic": True}
            case = {"schema": "platform.case", "id": cid, "tenant": T, "status": "open", "service": sid, "opened_at": iso(opened),
                    "opened_by": "user://citizens/demo-%04d" % R.randint(1, 4000), "title": "[demo] %s — application %s" % (cfg["title"], tag),
                    "workflow": "ds:airawatdeveloper:solution/declarative-service", "current_step": "scrutiny", "subjects": [], "scope": dict(scope)}
            t = opened
            # --- scrutiny (zone office) ---
            med = 108 if zone == "zone-4" else 34                                      # zone 4 is slow
            dur = hours(med)
            tsk = {"schema": "platform.task", "id": "ds:%s:task/%s-%s-scrutiny" % (T, sid, tag), "tenant": T, "case": cid, "verb": "review",
                   "title": "[demo] Scrutiny", "assignee": "role://%s/urban.dealing-assistant" % T, "workflow_step": "scrutiny",
                   "created_at": iso(t), "scope": dict(scope), "status": "open"}
            if t + dt.timedelta(hours=dur) < NOW:
                done = t + dt.timedelta(hours=dur)
                r = R.random()
                decision = "returned" if r < cfg["returns"] else "rejected" if r < cfg["returns"] + 0.05 else "approved"
                tsk.update(status="done", done_at=iso(done), decision=decision, completed_by="user://%s/city-da-assistant-z%s" % (T, zone[-1]))
                recs.append(tsk)
                if decision != "approved":
                    case.update(status="closed", current_step=decision, closed_at=iso(done))
                    if decision == "returned": case["scope"]["return_reason"] = wpick(RETURN_REASONS[sid])
                    recs.append(case); continue
                t = done
                # --- approval (estate officer) — a backlog builds in the last three weeks ---
                recent = (NOW - t).days < 21
                dur = hours(80 if recent else 20, 0.5)
                a = {"schema": "platform.task", "id": "ds:%s:task/%s-%s-approval" % (T, sid, tag), "tenant": T, "case": cid, "verb": "review",
                     "title": "[demo] Approval", "assignee": "role://%s/urban.estate-officer" % T, "workflow_step": "approval",
                     "created_at": iso(t), "scope": dict(scope), "status": "open"}
                case["current_step"] = "approval"
                if t + dt.timedelta(hours=dur) < NOW:
                    done = t + dt.timedelta(hours=dur)
                    a.update(status="done", done_at=iso(done), decision="approved", completed_by="user://%s/city-da-estate-officer" % T)
                    case.update(status="closed", current_step="resolved", closed_at=iso(done))
                    fee = cfg["fee"] or round(R.uniform(60, 400) * 150)
                    recs.append({"schema": "urban.payment", "id": "ds:%s:urban/payment/syn-%s" % (T, tag.lower()), "tenant": T,
                                 "receipt_no": "SYN-%s" % tag, "service": sid, "authority": T, "case": cid, "amount": fee, "currency": "INR",
                                 "method": R.choice(["upi", "upi", "card", "netbanking"]), "gateway": "sandbox (demo history)", "status": "paid",
                                 "paid_at": iso(opened + dt.timedelta(minutes=5)), "created_at": iso(opened)})
                recs.append(a)
            else:
                recs.append(tsk)
            recs.append(case)

by = {}
for r in recs: by[r["schema"]] = by.get(r["schema"], 0) + 1
openc = sum(1 for r in recs if r["schema"] == "platform.case" and r["status"] == "open")
print("generated:", by, "| open cases:", openc)
if MODE != "apply":
    sys.exit()
fp = json.load(open("/data/app-bundles/.secrets/fulfil-principals.json"))[T]
def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, "%s %s" % (e.code, e.read().decode()[:160])
tok = post(IDN + "/login", {"username": fp["username"], "tenant": T, "password": fp["password"]})[0]["access_token"]
errs = []
def put(o):
    r, e = post(K + "/v1/registry/objects", {"id": o["id"], "object": o}, tok)
    if e: errs.append((o["schema"], e))
with ThreadPoolExecutor(8) as ex: list(ex.map(put, recs))
print("written: %d, failed: %d" % (len(recs) - len(errs), len(errs)))
for s, e in errs[:3]: print("  e.g.", s, e)
