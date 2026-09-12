#!/usr/bin/env python3
"""news-gdelt — real, KEYLESS news ingestion from the GDELT DOC 2.0 API → news.item (News vertical, slice 1).

The global sibling of news-rss. Its ONLY I/O is the kernel broker:
  fetch  the GDELT DOC 2.0 article search (keyless JSON), egress-gated to api.gdeltproject.org
  write  news.item — one row per article, tagged with its jurisdiction + civic topics

For each query "term@jurisdiction" it hits
  https://api.gdeltproject.org/api/v2/doc/doc?query=<term>&mode=artlist&format=json&sort=datedesc&maxrecords=<n>&timespan=<t>
parses the JSON `articles[]`, infers civic topics from the title, stamps the jurisdiction, and writes a news.item
keyed by a stable hash of the article url (idempotent upsert — shares the id space with news-rss, so the same url
from both sources de-duplicates naturally).

REAL coverage only — fetch or fail loudly. NO synthetic fabrication, NO tenant default. GDELT coverage for a small
city can be sparse; an empty result is left empty, never fabricated. If EVERY query fails, the run exits non-zero.

  Env: TENANT (required) · QUERIES (required, comma-sep "term@jurisdiction"; jurisdiction defaults to TENANT)
       MAXITEMS (per query, default 30) · TIMESPAN (GDELT window, default 7d)
"""
import hashlib, json, os, re, time, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
QUERIES = os.environ.get("QUERIES", "").strip()
MAXITEMS = int(os.environ.get("MAXITEMS", "30"))
TIMESPAN = os.environ.get("TIMESPAN", "7d")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/gdelt"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

TOPIC_KW = {
    "water": ["water", "drainage", "sewage", "flood", "dam", "rain", "monsoon", "river", "waterlog"],
    "health": ["health", "hospital", "dengue", "malaria", "covid", "disease", "clinic", "medical", "vaccine"],
    "transport": ["traffic", "metro", "bus", "road", "transport", "railway", "parking", "highway", "flyover"],
    "safety": ["police", "crime", "accident", "fire", "safety", "stampede", "security", "rescue", "crowd"],
    "governance": ["municipal", "corporation", "commissioner", "council", "mayor", "budget", "tax", "election", "ward", "policy"],
    "environment": ["pollution", "air quality", "waste", "garbage", "tree", "green", "climate", "environment"],
    "infrastructure": ["construction", "project", "bridge", "building", "smart city", "development", "infrastructure"],
    "education": ["school", "college", "university", "student", "exam", "education"],
    "religion": ["kumbh", "temple", "festival", "pilgrim", "yatra", "mela", "trimbakeshwar"],
}


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


GDELT_DELAY = float(os.environ.get("GDELT_DELAY", "6"))       # GDELT DOC allows ~1 request / 5s — pace politely


def _fetch_body(url):
    r = call("/v1/fetch", {"url": url})
    body = (r or {}).get("body") or ""
    if not body and (r or {}).get("body_b64"):
        import base64, gzip
        raw = base64.b64decode(r["body_b64"])
        try: body = gzip.decompress(raw).decode("utf-8", "replace")
        except Exception: body = raw.decode("utf-8", "replace")
    return body


def fetch_json(url):
    """GDELT throttles to ~1 req/5s and returns a PLAIN-TEXT notice (not JSON) when exceeded — detect that and
    retry once after a pause. Real JSON starts with '{'."""
    for attempt in range(2):
        body = _fetch_body(url)
        s = body.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return {}
        if "limit requests" in body.lower() or not s:          # rate-limited / empty → back off and retry once
            if attempt == 0:
                time.sleep(GDELT_DELAY); continue
        return {}
    return {}


def topics_of(text):
    t = (text or "").lower()
    return [topic for topic, kws in TOPIC_KW.items() if any(k in t for k in kws)]


def gdelt_date_to_iso(s):
    # GDELT seendate: "20260909T043000Z"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(s, "%Y%m%dT%H%M%SZ"))
    except Exception:
        return NOW


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    if not QUERIES:
        raise SystemExit("FATAL: QUERIES not set — configure the jurisdiction search terms (comma-sep 'term@jurisdiction').")
    queries = [q.strip() for q in QUERIES.split(",") if q.strip()]
    seen, written, errors = set(), 0, 0
    for qi, spec in enumerate(queries):
        if qi:
            time.sleep(GDELT_DELAY)                             # GDELT: pace ≥5s between queries
        term, _, juris = spec.partition("@")
        term = term.strip(); juris = (juris.strip() or TENANT).lower()
        url = (GDELT + "?query=" + urllib.parse.quote(term)
               + f"&mode=artlist&format=json&sort=datedesc&maxrecords={MAXITEMS}&timespan={TIMESPAN}")
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"  ⚠ fetch failed for '{term}' ({type(e).__name__}: {str(e)[:80]})", flush=True); errors += 1; continue
        arts = data.get("articles") or []
        n = 0
        for a in arts:
            link = a.get("url") or ""
            title = (a.get("title") or "").strip()
            if not link or not title:
                continue
            fid = f"ds:{TENANT}:news/{hashlib.sha1(link.encode()).hexdigest()[:16]}"
            if fid in seen:
                continue
            seen.add(fid)
            fact = {
                "schema": "news.item", "id": fid, "tenant": TENANT,
                "title": title[:400], "summary": "", "url": link,
                "source": (a.get("domain") or "GDELT")[:120], "source_ref": SRC,
                "published_at": gdelt_date_to_iso(a.get("seendate") or ""), "observed_at": NOW,
                "language": (a.get("language") or "").lower()[:8] or "en",
                "topics": topics_of(title), "jurisdiction": juris,
                "image_url": a.get("socialimage") or "", "ingest": "news-gdelt",
            }
            try:
                call("/v1/write", {"scope": "news.item", "id": fid, "object": fact,
                                   "valid_time": {"from": NOW}})
                written += 1; n += 1
            except Exception as e:
                print(f"  ⚠ write failed {fid} ({str(e)[:120]})", flush=True); errors += 1
        print(f"  {term!r} @{juris}: {n} items (of {len(arts)} returned)", flush=True)
    print(f"wrote {written} news.item · {len(queries)} queries · {errors} errors", flush=True)
    print("RESULT: " + json.dumps({"source": "gdelt-doc-2.0", "items": written,
                                   "queries": len(queries), "errors": errors}))
    if written == 0:
        raise SystemExit("FATAL: 0 news items ingested from GDELT — every query failed or returned nothing.")


if __name__ == "__main__":
    main()
