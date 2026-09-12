#!/usr/bin/env python3
"""news-rss — real, KEYLESS news ingestion from Google News RSS → news.item (News vertical, slice 1).

Its ONLY I/O is the kernel broker:
  fetch  the Google News RSS search feed (keyless), egress-gated to news.google.com
  write  news.item — one row per article, tagged with its jurisdiction + civic topics

For each configured query "term@jurisdiction" it fetches
  https://news.google.com/rss/search?q=<term>&hl=<HL>&gl=<GL>&ceid=<CEID>
parses the RSS <item>s, strips the HTML snippet, infers civic topics from the text, stamps the jurisdiction, and
writes a news.item keyed by a stable hash of the article link (idempotent upsert). The Personal Agent's
`list_updates` tool then ranks these by the user's place (jurisdiction) + role (topics).

REAL feeds only — fetch or fail loudly. NO synthetic fabrication, NO tenant default. If every query fails / returns
nothing, the run exits non-zero rather than pretend success.

  Env: TENANT (required) · QUERIES (required, comma-sep "term@jurisdiction"; jurisdiction defaults to TENANT)
       MAXITEMS (per query, default 20) · HL (default en-IN) · GL (default IN) · CEID (default IN:en)
"""
import hashlib, html, json, os, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
QUERIES = os.environ.get("QUERIES", "").strip()
MAXITEMS = int(os.environ.get("MAXITEMS", "20"))
HL = os.environ.get("HL", "en-IN"); GL = os.environ.get("GL", "IN"); CEID = os.environ.get("CEID", "IN:en")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/google-news"
LANG = (HL.split("-")[0] or "en")

# civic topic tags — keyword → topic. Applied to title+summary (lowercased).
TOPIC_KW = {
    "water": ["water", "drainage", "sewage", "flood", "dam", "rain", "monsoon", "river", "waterlog", "drinking water"],
    "health": ["health", "hospital", "dengue", "malaria", "covid", "disease", "clinic", "medical", "vaccine"],
    "transport": ["traffic", "metro", "bus", "road", "transport", "railway", "parking", "highway", "flyover"],
    "safety": ["police", "crime", "accident", "fire", "safety", "stampede", "security", "rescue", "crowd"],
    "governance": ["municipal", "corporation", "commissioner", "council", "mayor", "budget", "tax", "election", "ward", "policy"],
    "environment": ["pollution", "air quality", "waste", "garbage", "tree", "green", "climate", "environment"],
    "infrastructure": ["construction", "project", "bridge", "building", "smart city", "development", "infrastructure"],
    "education": ["school", "college", "university", "student", "exam", "education"],
    "religion": ["kumbh", "temple", "festival", "pilgrim", "yatra", "mela", "trimbakeshwar"],
}
_TAG = re.compile(r"<[^>]+>")


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def fetch_text(url):
    """Fetch a URL through the broker; return the raw text body (RSS is text/xml → lands in 'body')."""
    r = call("/v1/fetch", {"url": url})
    if isinstance(r, dict):
        if r.get("body"):
            return r["body"]
        if r.get("body_b64"):                                   # (defensive: gzipped/binary edge)
            import base64, gzip
            raw = base64.b64decode(r["body_b64"])
            try:
                return gzip.decompress(raw).decode("utf-8", "replace")
            except Exception:
                return raw.decode("utf-8", "replace")
    return ""


def strip_html(s):
    return html.unescape(_TAG.sub(" ", s or "")).strip()


def topics_of(text):
    t = (text or "").lower()
    return [topic for topic, kws in TOPIC_KW.items() if any(k in t for k in kws)]


def to_iso(rfc822):
    try:
        dt = parsedate_to_datetime(rfc822)
        return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ") if dt else NOW
    except Exception:
        return NOW


def parse_feed(xml_text, jurisdiction):
    """Parse a Google News RSS document → list of news.item objects."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"  ⚠ parse error ({type(e).__name__}) — skipping this feed", flush=True)
        return out
    for item in root.iter("item"):
        def txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        title = strip_html(txt("title"))
        link = txt("link")
        if not title or not link:
            continue
        summary = strip_html(txt("description"))
        src_el = item.find("source")
        source = strip_html(src_el.text) if src_el is not None else "Google News"
        pub = txt("pubDate")
        fid = f"ds:{TENANT}:news/{hashlib.sha1(link.encode()).hexdigest()[:16]}"
        out.append({
            "schema": "news.item", "id": fid, "tenant": TENANT,
            "title": title[:400], "summary": summary[:1000], "url": link,
            "source": source[:120], "source_ref": SRC,
            "published_at": to_iso(pub), "observed_at": NOW, "language": LANG,
            "topics": topics_of(title + " " + summary), "jurisdiction": jurisdiction,
            "ingest": "news-rss",
        })
    return out


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    if not QUERIES:
        raise SystemExit("FATAL: QUERIES not set — configure the jurisdiction search terms (comma-sep 'term@jurisdiction').")
    queries = [q.strip() for q in QUERIES.split(",") if q.strip()]
    seen, written, errors = set(), 0, 0
    for spec in queries:
        term, _, juris = spec.partition("@")
        term = term.strip(); juris = (juris.strip() or TENANT).lower()
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(term)
               + f"&hl={HL}&gl={GL}&ceid={CEID}")
        try:
            xml_text = fetch_text(url)
        except Exception as e:
            print(f"  ⚠ fetch failed for '{term}' ({type(e).__name__}: {str(e)[:80]})", flush=True); errors += 1; continue
        items = parse_feed(xml_text, juris)[:MAXITEMS]
        n = 0
        for fact in items:
            if fact["id"] in seen:
                continue
            seen.add(fact["id"])
            try:
                call("/v1/write", {"scope": "news.item", "id": fact["id"], "object": fact,
                                   "valid_time": {"from": fact["observed_at"]}})
                written += 1; n += 1
            except Exception as e:
                print(f"  ⚠ write failed {fact['id']} ({str(e)[:120]})", flush=True); errors += 1
        print(f"  {term!r} @{juris}: {n} items", flush=True)
    print(f"wrote {written} news.item · {len(queries)} queries · {errors} errors", flush=True)
    print("RESULT: " + json.dumps({"source": "google-news-rss", "items": written,
                                   "queries": len(queries), "errors": errors}))
    if written == 0:
        raise SystemExit("FATAL: 0 news items ingested — every feed failed or returned nothing (fetch-or-fail).")


if __name__ == "__main__":
    main()
