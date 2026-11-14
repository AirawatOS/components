"""doc-ingest — knowledge ingest as a scheduled job (runs in the `documents` runtime).

A knowledge.ingest_request names a document — FILE (file_id: the original already in the filestore) or URL (an
official source) — with its title, issuer, class, legal force and an optional norm-extraction focus. Each run:

  1. NEW requests (status pending, or failed with attempts < 3), at most MAX_NEW_PER_RUN (1): get the original
     (filestore, or the URL directly — large PDFs exceed the broker's fetch cap), extract the text (pypdf), store
     the text (and a URL's original) in the filestore, write knowledge.document (status acquired) → acquired.
  2. IN-PROGRESS requests: read where the downstream engines have got to and record it —
       a current knowledge.version            → sectioned   (doc-chunk-version)
       every chunk of it has an embedding     → indexed     (embed-chunks)   — rule-extract picks these up
       rule-extract marked it rules_proposed  → done        (with the number of proposed rules)
Idempotent; every stage writes the request's status, counts and any error, so a restart resumes. Never fabricates
text: a PDF with no extractable text (a scan) fails with that reason.
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
CID, VER = "ds:airawatdeveloper:driver/doc-ingest", "0.1.0"
MAX_NEW = int(os.environ.get("MAX_NEW_PER_RUN") or 1)
MAX_ATTEMPTS = 3
UA = "AirawatOS/doc-ingest (+https://airawat.org)"
ALLOWED_HOSTS = {"www.mohua.gov.in", "mohua.gov.in"}           # = the declared egress


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def call(path, body, timeout=300):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(2 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(2 * (a + 1)); continue
            raise


def mine(scope, limit=20000):
    rows = [r.get("object") or r for r in (call("/v1/query", {"scope": scope, "limit": limit}).get("results") or [])]
    return [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % T)]


def write(scope, obj):
    t = now()
    call("/v1/write", {"scope": scope, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def put(raw):
    return call("/v1/file/put", {"content_b64": base64.b64encode(raw).decode()}, timeout=600)["id"]


def get_file(fid):
    return base64.b64decode(call("/v1/file/get", {"id": fid}, timeout=600)["content_b64"])


def fetch_url(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise RuntimeError("URL host %s is not in this driver's declared egress %s" % (host, sorted(ALLOWED_HOSTS)))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
        if (r.geturl() and (urllib.parse.urlparse(r.geturl()).hostname or "") not in ALLOWED_HOSTS):
            raise RuntimeError("redirected outside the declared egress: %s" % r.geturl())
        return r.read()


def extract_text(raw, fmt):
    if fmt == "pdf" or raw[:4] == b"%PDF":
        import pypdf
        rd = pypdf.PdfReader(io.BytesIO(raw))
        pages = []
        for i, p in enumerate(rd.pages):
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n\n".join(pages).strip(), len(rd.pages)
    if fmt in ("html", "htm"):
        import html as _h
        s = raw.decode("utf-8", "replace")
        s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
        return re.sub(r"\s+\n", "\n", _h.unescape(re.sub(r"(?s)<[^>]+>", " ", s))).strip(), None
    return raw.decode("utf-8", "replace").strip(), None


def slug(s):
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]) or "document"


def stage_new(req):
    """pending → acquired: the original + the extracted text in the filestore, and the knowledge.document."""
    att = int(req.get("attempts") or 0) + 1
    write("knowledge.ingest_request", dict(req, status="extracting", attempts=att, updated_at=now(), error=None))
    kind = req.get("source_kind") or ("url" if req.get("url") else "file")
    if kind == "url":
        raw = fetch_url(req["url"]); raw_id = put(raw)
    else:
        raw_id = req["file_id"]; raw = get_file(raw_id)
    fmt = (req.get("format") or ("pdf" if (raw[:4] == b"%PDF" or str(req.get("filename") or req.get("url") or "").lower().endswith(".pdf")) else "txt")).lower()
    text, pages = extract_text(raw, fmt)
    if len(text) < 200:
        raise RuntimeError("no readable text extracted (%d characters from a %s) — a scanned PDF needs OCR" % (len(text), fmt))
    text_id = put(text.encode("utf-8"))
    doc_id = req.get("doc_id") or slug(req.get("title"))
    t = now()
    doc = {"schema": "knowledge.document", "id": "ds:%s:knowledge/document/%s" % (T, doc_id), "doc_id": doc_id,
           "title": req.get("title") or "Untitled document", "doc_class": req.get("doc_class") or "guideline",
           "legal_force": req.get("legal_force") or "advisory", "issuer": req.get("issuer") or "",
           "jurisdiction": req.get("jurisdiction") or {"level": "national"},
           "citation_style": req.get("citation_style") or "paragraph",
           "source_blob": raw_id, "source_format": fmt, "source_filename": req.get("filename"), "source_bytes": len(raw),
           "format": "txt", "blob": text_id, "checksum": text_id,
           "text_derivation": {"from_blob": raw_id, "method": "extract-text(%s)" % fmt, "pages": pages,
                               "by": {"component": CID, "version": VER}, "at": t},
           "source": {"kind": kind, "url": req.get("url"), "filename": req.get("filename"), "added_by": req.get("requested_by"),
                      "request": req["id"], "retrieved_at": t, "driver": {"id": CID, "version": VER}},
           "produced_by": {"component": CID, "version": VER}, "status": "acquired", "current_version": None, "tenant": T}
    doc = {k: v for k, v in doc.items() if v is not None}
    write("knowledge.document", doc)
    write("knowledge.ingest_request", dict(req, status="acquired", attempts=att, doc_id=doc_id, doc_ref=doc["id"],
          counts={"pages": pages, "text_chars": len(text), "source_bytes": len(raw)}, updated_at=now(), error=None))
    print("  ✓ %s: %s pages, %d characters → %s" % (req.get("title"), pages, len(text), doc["id"]), flush=True)


def reconcile(reqs, docs, versions, chunks, embeds, rules):
    """Record where each in-progress document has got to in the downstream engines."""
    by_doc = {d.get("doc_id"): d for d in docs}
    vers = {v.get("id"): v for v in versions}
    emb_hashes = {e.get("chunk_hash") for e in embeds}
    for r in reqs:
        if r.get("status") not in ("acquired", "sectioned", "indexed", "rules_proposed"):
            continue
        d = by_doc.get(r.get("doc_id"))
        if not d:
            continue
        st, counts = r["status"], dict(r.get("counts") or {})
        v = vers.get(d.get("current_version"))
        if v:
            hashes = [c.get("chunk_hash") for c in (v.get("chunks") or [])]
            counts["sections"] = len(hashes)
            counts["indexed"] = sum(1 for h in hashes if h in emb_hashes)
            if st == "acquired":
                st = "sectioned"
            if st == "sectioned" and hashes and counts["indexed"] >= len(hashes):
                st = "indexed"                                   # rule-extract picks this up
        if st == "rules_proposed":
            counts["rules_proposed"] = sum(1 for x in rules if (x.get("source") or {}).get("doc") == d["id"])
            st = "done"
        if st != r["status"] or counts != (r.get("counts") or {}):
            write("knowledge.ingest_request", dict(r, status=st, counts=counts, updated_at=now()))
            print("  · %s: %s → %s %s" % (r.get("title"), r["status"], st, counts), flush=True)


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    reqs = mine("knowledge.ingest_request")
    new = [r for r in reqs if r.get("status") == "pending"
           or (r.get("status") == "failed" and int(r.get("attempts") or 0) < MAX_ATTEMPTS and not r.get("doc_ref"))]
    print("tenant=%s · %d requests · %d new" % (T, len(reqs), len(new)), flush=True)
    done_new = 0
    for r in sorted(new, key=lambda x: str(x.get("requested_at") or ""))[:MAX_NEW]:
        try:
            stage_new(r); done_new += 1
        except Exception as e:
            att = int(r.get("attempts") or 0) + 1
            write("knowledge.ingest_request", dict(r, status="failed", stage="extract", attempts=att,
                                                   error=str(e)[:300], updated_at=now()))
            print("  ✗ %s (attempt %d): %s" % (r.get("title"), att, str(e)[:200]), flush=True)
    active = [r for r in mine("knowledge.ingest_request") if r.get("status") in ("acquired", "sectioned", "indexed", "rules_proposed")]
    if active:
        reconcile(active, mine("knowledge.document", 2000), mine("knowledge.version", 5000),
                  [], mine("knowledge.embedding", 50000), mine("governance.rule", 10000))
    print("RESULT: " + json.dumps({"extracted": done_new, "in_progress": len(active)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
