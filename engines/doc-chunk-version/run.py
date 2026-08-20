#!/usr/bin/env python3
"""doc-chunk-version engine: knowledge.document blob → content-addressed knowledge.chunk[] + knowledge.version.

Confined — only I/O is the broker. For each acquired document it: reads the raw blob (/v1/file/get),
chunks it STRUCTURE-AWARE by the doc's `citation_style` (stage/section/else window), CONTENT-ADDRESSES each
chunk (chunk_hash = sha2-256(text); stored via /v1/file/put; a knowledge.chunk record whose id derives from
the hash → identical text deduped + SHARED across versions), and writes a knowledge.version whose ordered
manifest of chunk hashes yields a `root_hash` that IS the version identity. Idempotent: if the recomputed
root_hash equals the current version's, no new version is minted. A changed doc mints new blobs only for
the changed chunks — unchanged chunks are shared (git-tree; storage ∝ change).
"""
import base64, hashlib, json, os, re, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")
ONLY_DOC = os.environ.get("DOC_ID")                       # a specific doc_id, or all acquired/chunked docs


def _call(path, body, timeout=40):
    # Retry transient broker/filestore errors (5xx / connection) so a momentary blip during a large write
    # doesn't abort the whole pipeline. 4xx (auth/capability) raise immediately — those are not transient.
    import time as _t
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


def _sha(b):
    return "sha2-256:" + hashlib.sha256(b).hexdigest()


def chunk_text(text, style):
    """Return [(locator_label, chunk_text)] — structure-aware where the doc's citation_style tells us how."""
    if style == "stage":
        parts, cur, label = [], [], "preamble"
        for line in text.splitlines():
            m = re.match(r"^\s*(Stage\s+[IVX]+)", line)
            if m:
                if cur:
                    parts.append((label, "\n".join(cur).strip()))
                label, cur = m.group(1), [line]
            else:
                cur.append(line)
        if cur:
            parts.append((label, "\n".join(cur).strip()))
        return [(l, t) for l, t in parts if t]
    if style == "section":
        parts = re.split(r"(?m)^(?=\s*(?:Section|Sec\.|Clause|\d+\.)\s)", text)
        return [(f"part {i}", p.strip()) for i, p in enumerate(parts) if p.strip()]
    # default: character window with overlap
    size, overlap, out, i = 1000, 200, [], 0
    while i < len(text):
        out.append((f"chars {i}-{min(i+size, len(text))}", text[i:i + size]))
        i += max(size - overlap, 1)
    return [(l, t) for l, t in out if t.strip()]


def main():
    # When invoked for a specific doc right after it was acquired, tolerate a brief read-after-write lag:
    # re-query a few times before giving up (a freshly-written knowledge.document may not be query-visible yet).
    import time as _t
    docs = []
    for attempt in range(6):
        alld = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.document", "limit": 1000}).get("results", [])]
        docs = [d for d in alld if d.get("doc_id") == ONLY_DOC] if ONLY_DOC else alld
        if docs or not ONLY_DOC:
            break
        _t.sleep(1.5)
    if not docs:
        print(f"no knowledge.document to chunk{f' for {ONLY_DOC!r}' if ONLY_DOC else ''}."); return 0
    versions = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.version", "limit": 2000}).get("results", [])]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for doc in docs:
        did = doc["doc_id"]
        got = _call("/v1/file/get", {"id": doc["blob"]})
        text = base64.b64decode(got["content_b64"]).decode("utf-8", "replace")
        pieces = chunk_text(text, doc.get("citation_style", "paragraph"))

        manifest = []
        for idx, (label, ctext) in enumerate(pieces):
            cb = ctext.encode("utf-8"); chash = _sha(cb)
            _call("/v1/file/put", {"content_b64": base64.b64encode(cb).decode()})   # content-addressed store (dedup)
            cid = f"ds:{T}:knowledge/chunk/{chash.split(':')[1][:16]}"
            _call("/v1/write", {"scope": "knowledge.chunk", "id": cid, "object": {
                "schema": "knowledge.chunk", "id": cid, "chunk_hash": chash, "doc_id": did, "text": ctext,
                "locator": {"kind": doc.get("citation_style", "paragraph"), "label": label},
                "word_count": len(ctext.split()), "tenant": T}, "valid_time": {"from": now}, "asserted_at": now})
            manifest.append({"chunk_hash": chash, "index": idx, "locator": {"kind": doc.get("citation_style", "paragraph"), "label": label}})

        root = _sha("".join(m["chunk_hash"] for m in manifest).encode())
        prior = sorted([v for v in versions if v.get("doc_id") == did], key=lambda v: v.get("version_no", 0))
        cur = prior[-1] if prior else None
        if cur and cur.get("root_hash") == root:
            print(f"  {did}: unchanged (root {root[:22]}…) — no new version"); continue

        vno = (cur["version_no"] + 1) if cur else 1
        vid = f"ds:{T}:knowledge/version/{did}-v{vno}"
        _call("/v1/write", {"scope": "knowledge.version", "id": vid, "object": {
            "schema": "knowledge.version", "id": vid, "doc_id": did, "version_no": vno, "root_hash": root,
            "chunk_count": len(manifest), "chunks": manifest, "supersedes": cur["id"] if cur else None,
            "created_at": now, "tenant": T}, "valid_time": {"from": now}, "asserted_at": now})

        doc["current_version"] = vid; doc["status"] = "chunked"
        _call("/v1/write", {"scope": "knowledge.document", "id": doc["id"], "object": doc,
                            "valid_time": {"from": now}, "asserted_at": now})

        prev_hashes = {c["chunk_hash"] for c in (cur.get("chunks", []) if cur else [])}
        now_hashes = {m["chunk_hash"] for m in manifest}
        shared = len(now_hashes & prev_hashes); new = len(now_hashes - prev_hashes)
        share_note = f" · {shared} shared + {new} new vs v{cur['version_no']}" if cur else ""
        print(f"  {did} v{vno}: {len(manifest)} chunks [{', '.join(m['locator']['label'] for m in manifest)}]"
              f" · root {root[:22]}…{share_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
