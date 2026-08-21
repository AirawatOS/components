#!/usr/bin/env python3
"""doc-chunk-version engine: knowledge.document blob → content-addressed knowledge.chunk[] + knowledge.version.

Confined — only I/O is the broker. For each acquired document it: reads the raw blob (/v1/file/get),
chunks it STRUCTURE-AWARE by the doc's `citation_style` (eid/stage/section/else window), CONTENT-ADDRESSES
each chunk (chunk_hash = sha2-256(text); stored via /v1/file/put; a knowledge.chunk record whose id derives
from the hash → identical text deduped + SHARED across versions), and writes a knowledge.version whose
ordered manifest of chunk hashes yields a `root_hash` that IS the version identity. Idempotent: if the
recomputed root_hash equals the current version's, no new version is minted. A changed doc mints new blobs
only for the changed chunks — unchanged chunks are shared (git-tree; storage ∝ change).

CONTENT-ANCHORED LOCATORS (ADR-0057). Instead of byte offsets, a chunk's `locator` is an eId derived from
the document's OWN numbering (4.11.1 -> chp_4__sec_4-11__para_4-11-1) plus a resolvable FRBR-style `uri`.
An eId is stable across re-versioning where a byte offset drifts. The eId scheme is DETERMINISTIC (no LLM):
regex on the numbering, deterministic PDF line-wrap repair, and the doc's own `[Action:...]` markers lifted
to a `responsibility` list. An OPTIONAL LLM fallback (env SEGMENT_ENRICH=1) handles documents that lack
usable numbering, where structure must be inferred — off by default so the engine stays deterministic.
"""
import base64, hashlib, json, os, re, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")
ONLY_DOC = os.environ.get("DOC_ID")                       # a specific doc_id, or all acquired/chunked docs
SEGMENT_ENRICH = os.environ.get("SEGMENT_ENRICH", "0") == "1"   # opt-in LLM fallback for UNstructured docs
SEGMENT_MODEL = os.environ.get("SEGMENT_MODEL", "claude-sonnet-5")


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


# -- content-anchored eId helpers (ADR-0057) ---------------------------------------------------------
_NUM = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\s+(?=\S)")     # a line OPENING a numbered unit: 4 / 4.11 / 4.11.1
_ACTION = re.compile(r"\[Action:\s*([^\]]+)\]")           # the doc's own responsibility marker


def _eid_of(number):
    """4.11.1 -> chp_4__sec_4-11__para_4-11-1  (content-anchored: tied to numbering, not byte position)."""
    p = number.split(".")
    parts = [f"chp_{p[0]}"]
    if len(p) >= 2: parts.append(f"sec_{p[0]}-{p[1]}")
    if len(p) >= 3: parts.append("para_" + "-".join(p))
    if len(p) >= 4: parts.append("pt_" + "-".join(p))
    return "__".join(parts)


def _frbr_uri(doc, eid):
    """Resolvable FRBR-style anchor, VERSION-AGNOSTIC: Work path + eId fragment. Chunks are shared across
    versions (content-addressed), so the stored anchor carries the stable Work+eId; the concrete Expression
    (…/eng@v<n>) is composed at citation time from the version context the retrieval envelope already holds.
    Work id from the doc's frbr_work if set, else derived from doc_id."""
    work = doc.get("frbr_work") or f"/akn/doc/{doc['doc_id'].strip('-')}"
    return f"{work}#{eid}"


def _clean(text):
    """Repair PDF line-wrap deterministically: de-hyphenate lowercase syllable breaks, collapse reflow
    newlines to spaces. Good enough for a citation anchor + retrieval; the LLM fallback does more."""
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)    # "consider-\nation" -> "consideration"
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)           # reflow line-breaks -> spaces
    return re.sub(r"\s{2,}", " ", text).strip()


def _valid_opener(number, state):
    """CHAPTER-MONOTONIC outline validation — the guard that separates real section numbers from PDF noise
    (prose decimals like "0.95", measurements like "2.5 m", TOC page-numbers, gigantic OCR fragments like
    "77370.50"). Chapters only ever advance by 1 and never repeat, so: a candidate in the CURRENT chapter is
    a section/para; chapter+1 opens the next chapter; anything else (a backward jump, chapter 0, chapter>99)
    is noise. Mutates `state` on a chapter advance."""
    try:
        segs = [int(p) for p in number.split(".")]
    except ValueError:
        return False
    c0 = segs[0]
    if len(segs) < 2 or c0 < 1 or c0 > 99 or any(s < 0 or s > 199 for s in segs[1:]):
        return False                                          # bare integers (list items, page-nos, table cells) are noise
    if c0 == state["chapter"]:
        return segs[1] >= 1                                   # within-chapter: a section/para N.M[.K[.L]]
    if c0 == state["chapter"] + 1 and segs[1] == 1:
        state["chapter"] = c0                                 # advance to the next chapter, introduced by its N.1 section
        return True
    return False


def _parse_units(text):
    """Split into numbered units by the document's OWN decimal numbering, robust to PDF-extraction noise.
    The body starts at the first THREE-part opener (a TOC lists only to section level, so this skips the
    duplicated table of contents + title page into one preamble chunk). Returns [(number, raw_text)]."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if (_NUM.match(ln) and _NUM.match(ln).group(1).count(".") >= 2)), 0)
    m0 = _NUM.match(lines[start]) if start < len(lines) else None
    state = {"chapter": int(m0.group(1).split(".")[0])} if m0 else {"chapter": 1}
    units, cur_no, buf = [], None, []
    for line in lines[start:]:
        m = _NUM.match(line)
        if m and _valid_opener(m.group(1), state):
            if cur_no is not None:
                units.append((cur_no, "\n".join(buf)))
            cur_no, buf = m.group(1), [line]
        else:
            buf.append(line)
    if cur_no is not None:
        units.append((cur_no, "\n".join(buf)))
    out = []
    preamble = "\n".join(lines[:start]).strip()
    if preamble:
        out.append(("__preamble__", preamble))
    out.extend((n, t) for n, t in units if t.strip())
    return out


def chunk_eid(text, doc):
    """Content-anchored segmentation (ADR-0057). Each numbered unit -> a chunk with an eId locator, cleaned
    text, a heading (section-level), and any [Action:...] lifted to responsibility. Returns [(locator, text)]
    where locator is the rich dict written to knowledge.chunk.locator. Returns None if the doc has no usable
    numbering (caller falls back to the window chunker / LLM)."""
    units = _parse_units(text)
    numbered = [u for u in units if u[0] != "__preamble__"]
    if len(numbered) < 5:                                  # not a numbered document — let the caller fall back
        return None
    pieces = []
    for number, raw in units:
        if number == "__preamble__":
            pieces.append(({"kind": "eid", "eid": "front", "number": None,
                            "uri": _frbr_uri(doc, "front")}, _clean(raw)[:2000]))
            continue
        actions = _ACTION.findall(raw)
        body = _ACTION.sub("", raw)
        clean = _clean(body)
        eid = _eid_of(number)
        loc = {"kind": "eid", "eid": eid, "number": number,
               "path": ["__".join(_eid_of(number).split("__")[:i + 1]) for i in range(number.count(".") + 1)],
               "uri": _frbr_uri(doc, eid)}
        # section-level units (chp/sec) contribute a heading; deeper units are the substantive clauses
        if number.count(".") <= 1:
            loc["heading"] = re.sub(r"\s+\d+\s*$", "", clean).strip()[:80]   # drop trailing TOC page-no
        if actions:
            loc["responsibility"] = [b.strip() for a in actions for b in re.split(r",| and ", a) if b.strip()]
        pieces.append((loc, clean))
    return pieces


def _llm_segment(text, doc):
    """OPTIONAL fallback for documents WITHOUT usable numbering (SEGMENT_ENRICH=1). One bounded broker
    /v1/complete call per window asks the model to emit structural units it can title + eId. Best-effort:
    any failure returns None so the caller uses the plain window chunker. NOTE: never send `temperature`
    to claude-sonnet-5 (deprecated → the gateway 502s)."""
    out, i, size = [], 0, 6000
    while i < len(text):
        seg = text[i:i + size]; i += size
        prompt = ("Segment this document text into structural units for citation. Return ONLY a JSON array "
                  '[{"eid":"<slug>","heading":"<short>","text":"<clean prose>"}] — eid a short kebab slug from '
                  "the heading, text with PDF line-wrap repaired. No prose outside the JSON.\n\n" + seg)
        try:
            r = _call("/v1/complete", {"model": SEGMENT_MODEL, "max_tokens": 1500,
                                       "messages": [{"role": "user", "content": prompt}]}, timeout=58)
            content = r.get("content") or r["choices"][0]["message"]["content"]
            arr = json.loads(re.search(r"\[.*\]", content, re.S).group(0))
        except Exception as e:
            print(f"    [enrich] window @{i} failed ({e}) — plain window fallback"); return None
        for u in arr:
            out.append(({"kind": "eid", "eid": u.get("eid", "unit"), "number": None,
                         "heading": u.get("heading", "")[:80],
                         "uri": _frbr_uri(doc, u.get("eid", "unit"))}, (u.get("text") or "").strip()))
    return [p for p in out if p[1]] or None


def chunk_text(text, style, doc=None):
    """Return [(locator, chunk_text)] — locator is a dict written verbatim to knowledge.chunk.locator.
    citation_style eid|paragraph tries the content-anchored eId segmenter first (ADR-0057)."""
    doc = doc or {}
    if style in ("eid", "paragraph"):
        eid = chunk_eid(text, doc)
        if eid:
            return eid
        if SEGMENT_ENRICH:                                 # unstructured doc + enrichment opted in
            llm = _llm_segment(text, doc)
            if llm:
                return llm
        # else fall through to the window chunker below
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
        return [({"kind": "stage", "label": l}, t) for l, t in parts if t]
    if style == "section":
        parts = re.split(r"(?m)^(?=\s*(?:Section|Sec\.|Clause|\d+\.)\s)", text)
        return [({"kind": "section", "label": f"part {i}"}, p.strip()) for i, p in enumerate(parts) if p.strip()]
    # default: character window with overlap (the honest fallback for unstructured text)
    size, overlap, out, i = 1000, 200, [], 0
    while i < len(text):
        out.append(({"kind": "window", "label": f"chars {i}-{min(i+size, len(text))}"}, text[i:i + size]))
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
        pieces = chunk_text(text, doc.get("citation_style", "paragraph"), doc)
        kinds = {loc.get("kind") for loc, _ in pieces}

        manifest = []
        for idx, (locator, ctext) in enumerate(pieces):
            cb = ctext.encode("utf-8"); chash = _sha(cb)
            _call("/v1/file/put", {"content_b64": base64.b64encode(cb).decode()})   # content-addressed store (dedup)
            cid = f"ds:{T}:knowledge/chunk/{chash.split(':')[1][:16]}"
            _call("/v1/write", {"scope": "knowledge.chunk", "id": cid, "object": {
                "schema": "knowledge.chunk", "id": cid, "chunk_hash": chash, "doc_id": did, "text": ctext,
                "locator": locator,                        # rich locator (eId + uri + heading/responsibility) — ADR-0057
                "word_count": len(ctext.split()), "tenant": T}, "valid_time": {"from": now}, "asserted_at": now})
            manifest.append({"chunk_hash": chash, "index": idx, "locator": locator})

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
        anchors = [m["locator"].get("eid") or m["locator"].get("label") or "?" for m in manifest]
        preview = ", ".join(anchors[:6]) + (f", …(+{len(anchors)-6})" if len(anchors) > 6 else "")
        print(f"  {did} v{vno}: {len(manifest)} chunks · locator={'/'.join(sorted(kinds))} [{preview}]"
              f" · root {root[:22]}…{share_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
