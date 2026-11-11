"""News solution setup — create the tenant's two news-ingest schedules (Google News RSS + GDELT) with a
jurisdiction-derived default QUERIES, so News starts flowing right after install. Idempotent; tune QUERIES later
by editing the platform.schedule env. Runs under the executor (TENANT + RUN_TOKEN + AIRAWAT_API=broker). NO
fabrication — it only writes config; the drivers fetch REAL feeds when they run."""
import os, json, urllib.request as u

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""


def bwrite(oid, obj):
    req = u.Request(API + "/v1/write", data=json.dumps({"scope": obj["schema"], "id": oid, "object": obj}).encode(),
                    headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(u.urlopen(req, timeout=30).read() or b"{}")


def bquery(scope):
    req = u.Request(API + "/v1/query", data=json.dumps({"scope": scope}).encode(),
                    headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return [r.get("object", r) for r in json.loads(u.urlopen(req, timeout=30).read() or b"{}").get("results", [])]


def city_name(aois, tenant):
    """The CITY to search news for: the tenant's AOI name (or its id's last segment — "…/space/aoi/bengaluru" →
    "Bengaluru"), else the tenant id. A tenant id is an organisation handle, not a place: searching "Janaagraha civic"
    returned news about the organisation, not the city."""
    for a in aois or []:
        n = a.get("name") or str(a.get("id") or "").rsplit("/", 1)[-1]
        if n:
            return n.replace("-", " ").strip().title()
    return tenant.replace("-", " ").title()


def queries(city, tenant):
    return "%s@%s,%s civic@%s,%s pollution@%s,%s water@%s,%s traffic@%s,%s flooding@%s,India civic@india" % (
        city, tenant, city, tenant, city, tenant, city, tenant, city, tenant, city, tenant)


def main():
    if not TENANT:
        print("News setup — no target tenant (self-test)."); return 0            # dev-Test / sanity run
    try:
        aois = bquery("space.aoi")
    except Exception:
        aois = []
    city = city_name(aois, TENANT)
    q = queries(city, TENANT)
    scheds = [("news-rss", "ds:airawat:driver/news-rss", "News ingest (Google News RSS)", "0 */3 * * *"),
              ("news-gdelt", "ds:airawat:driver/news-gdelt", "News ingest (GDELT DOC 2.0)", "30 */3 * * *")]
    for name, cid, label, cron in scheds:
        sid = "ds:%s:platform/schedule/%s" % (TENANT, name)
        obj = {"schema": "platform.schedule", "id": sid, "tenant": TENANT, "name": label, "component": cid,
               "cron": cron, "enabled": True, "overlap": "skip", "env": {"QUERIES": q, "MAXITEMS": "20"}}
        bwrite(sid, obj); print("schedule ready:", sid)
    print("news schedules ready for %s · QUERIES=%s" % (TENANT, q)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
