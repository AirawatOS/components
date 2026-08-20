#!/usr/bin/env python3
"""doc-acquire driver: a document → the ORIGINAL bytes preserved as-is in the filestore + a knowledge.document.

Confined — its only I/O is the kernel broker. Platform rule: the *original input is kept exactly as received*
(source_blob) so everything downstream traces back to (1) that input file, (2) this driver, and (3) the person
who supplied it. The extracted plain text (used for sectioning/embedding) is stored as a SEPARATE derived blob
(text_blob), with the document recording that it was derived FROM source_blob and by what method. Both blobs
are content-addressed (id IS the sha2-256), so integrity is inherent. Fails loudly if the source is empty.

Inputs (env):
  SOURCE_RAW_FILE     path to the original bytes as received (PDF/HTML/txt/…)  — preferred, kept as-is
  SOURCE_RAW_FORMAT   original format (pdf|html|txt|md)
  SOURCE_RAW_FILENAME original filename (uploads)
  SOURCE_TEXT_FILE    path to the extracted plain text (the working/derived text)   — preferred
  SOURCE_TEXT_B64     small inline text (fallback when no file)
  SOURCE_URL          fetch the original via the broker (legacy path; egress-gated)
  SOURCE_ORIGIN(json) provenance {kind, url|filename, added_by(person), orig_format, retrieved_at}
  DOC_TITLE, DOC_CLASS, LEGAL_FORCE, ISSUER, JURISDICTION(json), CITATION_STYLE, EFFECTIVE_FROM, DOC_ID
"""
import base64, hashlib, json, os, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")
CID = "ds:airawat:driver/doc-acquire"
VER = "0.2.0"


def _call(path, body, timeout=60):
    # Retry transient broker/filestore errors (5xx / connection) — large originals can momentarily 500.
    import time as _t, urllib.error
    for _a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise
        except urllib.error.URLError:
            if _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise


def _put(raw):
    r = _call("/v1/file/put", {"content_b64": base64.b64encode(raw).decode()})
    return r["id"], r.get("deduped")


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        source = json.loads(os.environ.get("SOURCE_ORIGIN") or "") or {}
    except Exception:
        source = {}

    # 1) THE ORIGINAL INPUT, kept exactly as received ------------------------------------------------
    original = None; orig_fmt = os.environ.get("SOURCE_RAW_FORMAT") or os.environ.get("FORMAT", "txt")
    raw_path = os.environ.get("SOURCE_RAW_FILE")
    url = os.environ.get("SOURCE_URL")
    if raw_path and os.path.exists(raw_path):
        original = open(raw_path, "rb").read()
        source.setdefault("filename", os.environ.get("SOURCE_RAW_FILENAME"))
    elif url:                                                    # legacy: the driver fetches the original itself
        r = _call("/v1/fetch", {"url": url})
        if r.get("upstream_status") != 200 or not r.get("body"):
            print(f"FAIL: fetch {url} → {r.get('upstream_status')}. Nothing written."); return 1
        original = r["body"].encode("utf-8"); source.setdefault("url", url)
        orig_fmt = os.environ.get("SOURCE_RAW_FORMAT", "html" if "html" in url.lower() else "txt")
    source.setdefault("retrieved_at", now)
    source.setdefault("kind", "url" if url else "upload")

    # 2) THE DERIVED TEXT used for processing (may equal the original for plain text) -----------------
    text = None
    tpath = os.environ.get("SOURCE_TEXT_FILE")
    if tpath and os.path.exists(tpath):
        text = open(tpath, "rb").read()
    elif os.environ.get("SOURCE_TEXT_B64"):
        text = base64.b64decode(os.environ["SOURCE_TEXT_B64"])
    if original is None:                                         # no separate original supplied → text IS the input
        original = text
    if original is None:
        print("FAIL: no input (set SOURCE_RAW_FILE / SOURCE_TEXT_FILE / SOURCE_TEXT_B64 / SOURCE_URL)."); return 1
    if text is None:
        text = original                                         # plain-text upload: derived == original

    # store both, content-addressed; the original is immutable and never mutated
    source_blob, sdedup = _put(original)
    text_blob, _ = _put(text)

    doc_id = os.environ.get("DOC_ID") or hashlib.sha256(source_blob.encode()).hexdigest()[:12]
    # who + which driver: the person is carried in source.added_by; the driver + run are ALSO stamped into the
    # registry write-provenance ledger (ADR-0052) from this run's credential — belt and braces for traceability.
    source.setdefault("driver", {"id": CID, "version": VER, "run": os.environ.get("AIROS_RUN_ID")})
    doc = {"schema": "knowledge.document", "id": f"ds:{T}:knowledge/document/{doc_id}", "doc_id": doc_id,
           "title": os.environ.get("DOC_TITLE", "Untitled document"),
           "doc_class": os.environ.get("DOC_CLASS", "reference"),
           "legal_force": os.environ.get("LEGAL_FORCE", "reference"),
           "issuer": os.environ.get("ISSUER", ""),
           "jurisdiction": json.loads(os.environ.get("JURISDICTION", "{}") or "{}"),
           "citation_style": os.environ.get("CITATION_STYLE", "paragraph"),
           # the original input, preserved as-is:
           "source_blob": source_blob, "source_format": orig_fmt,
           "source_filename": source.get("filename"), "source_bytes": len(original),
           # the working text derived from it (what chunk/embed read):
           "format": "txt", "blob": text_blob, "checksum": text_blob,
           "text_derivation": {"from_blob": source_blob, "method": f"extract-text({orig_fmt})",
                               "by": {"component": CID, "version": VER}, "at": now},
           "source": source, "produced_by": {"component": CID, "version": VER},
           "status": "acquired", "current_version": None, "tenant": T}
    if os.environ.get("EFFECTIVE_FROM"):
        doc["effective_from"] = os.environ["EFFECTIVE_FROM"]
    _call("/v1/write", {"scope": "knowledge.document", "id": doc["id"], "object": doc,
                        "valid_time": {"from": now}, "asserted_at": now})
    print(f"acquired '{doc['title']}' [{doc['doc_class']} · {doc['legal_force']}] · "
          f"ORIGINAL {source_blob} ({len(original)} bytes, {orig_fmt}, deduped={sdedup}) kept as-is; "
          f"text→{text_blob} ({len(text)} bytes) derived. by={source.get('added_by')} · {doc['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
