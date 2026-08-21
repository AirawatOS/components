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
_NUM = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+(?=\S)")  # a numbered-unit opener; trailing dot optional (4.11.1 or 1.5.1.)
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
    """Repair PDF extraction artifacts deterministically so a chunk reads naturally:
      1. de-hyphenate lowercase syllable breaks ("consider-\\nation" -> "consideration");
      2. reflow hard line-wrap into spaces;
      3. DE-LETTER-SPACE — some PDFs emit justified text as "N u m e r o u s  l a w s" (one space between
         letters WITHIN a word, two BETWEEN words). Collapse a run of 3+ single-space-separated letters back
         into a word; the wider word gaps survive step 4. Only fully letter-spaced runs match, so normal
         prose is untouched.
    The LLM fallback does more for genuinely broken text; this makes the common cases readable at no cost."""
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)
    # De-letter-space. Some PDFs emit justified text as "N u m e r o u s" — a regular space BETWEEN LETTERS
    # of a word, and a WIDER separator (non-breaking space U+00A0, a double space, or a line-break) BETWEEN
    # WORDS. Tokenise on those wide word-boundaries, then collapse only tokens that are WHOLLY single chars
    # joined by single spaces. A normal phrase like "with a runoff" is one token containing multi-char words,
    # so it is left untouched — no corruption of clean prose.
    tokens = [t.strip() for t in re.split(r"[\xa0]+|[ ]{2,}|\s*\n\s*", text) if t.strip()]
    text = " ".join(t.replace(" ", "") if re.fullmatch(r"\w( \w)+", t) else t for t in tokens)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _sane_number(number):
    """Reject numbers that can't be a real section: chapter 0 or >99, or a bare integer, or an x.0-style
    page-fraction (sections/paras start at 1). Returns the int segments, or None."""
    try:
        segs = [int(p) for p in number.split(".")]
    except ValueError:
        return None
    if len(segs) < 2 or not (1 <= segs[0] <= 99) or any(s < 1 or s > 199 for s in segs[1:]):
        return None
    return segs


def _toc_like(rest):
    """A table-of-contents entry: a short heading followed by a lone trailing page number
    ('Perspective Plan  7'). Body openers are followed by prose, not a bare page number."""
    return bool(re.search(r"[^\d.]\s+\d{1,4}\s*$", rest)) and len(rest.split()) <= 16


def _openers(lines):
    """Detect the numbered-unit openers in document order, robust to PDF noise. Two conventions are handled
    by DETECTING which the document uses: some mark sections with a trailing dot ('1.5.1.'), others do not
    ('4.11.1'). TOC entries (trailing page number) are excluded so they fold into surrounding text rather
    than becoming empty chunks. Returns [(line_index, number)]."""
    cand = []
    for i, ln in enumerate(lines):
        m = _NUM.match(ln)
        if not m or not _sane_number(m.group(1)):
            continue
        had_dot = bool(re.match(r"^\s*\d+(?:\.\d+){0,3}\.\s", ln))
        cand.append((i, m, ln[m.end():].rstrip(), had_dot))
    if not cand:
        return []
    trailing_dot = sum(1 for c in cand if c[3]) / len(cand) > 0.6      # the doc's convention
    out = []
    for i, m, rest, had_dot in cand:
        if _toc_like(rest):
            continue
        # suppress prose decimals ("2.5 m", "1.33 ECS"): in a trailing-dot doc a real opener HAS the dot;
        # in a no-dot doc a real opener starts a Capitalised heading/word.
        if (had_dot if trailing_dot else rest[:1].isupper()):
            out.append((i, m.group(1)))
    return out


def _parse_units(text):
    """Partition the document into numbered units in order — the RECONSTRUCTION contract: preamble + every
    unit, concatenated, is the whole document (nothing dropped). Text between opener k and opener k+1 is
    unit k's body; everything before the first opener is the preamble (title page + TOC fold in here).
    Returns [] if too little structure is found, so the caller falls back to the window chunker."""
    lines = text.splitlines()
    ops = _openers(lines)
    if len(ops) < 5:
        return []
    # Fold the front matter (title page + table of contents) into the preamble. A TOC restates the body's
    # numbering, so a few TOC lines that escape the per-line filter survive as openers BEFORE the body — but
    # they SCATTER across chapters (…3.5.3 → 5.6 → …10.4.2), whereas the body increments smoothly (1.1, 1.2,
    # …, 2.1). Find the first opener that begins a locally-smooth run (consecutive chapter step ≤ 1); fold
    # everything before it into the preamble. (Reconstruction still holds — nothing is dropped.)
    def _ch(n):
        return int(n.split(".")[0])
    body_start, need = 0, min(8, len(ops) // 2)
    for k in range(len(ops)):
        run = 1
        for j in range(k + 1, min(k + 12, len(ops))):
            if abs(_ch(ops[j][1]) - _ch(ops[j - 1][1])) <= 1:
                run += 1
            else:
                break
        if run >= need:
            body_start = k; break
    body = ops[body_start:]
    out = []
    pre = "\n".join(lines[:body[0][0]]).strip()
    if pre:
        out.append(("__preamble__", pre))
    for k, (i, num) in enumerate(body):
        end = body[k + 1][0] if k + 1 < len(body) else len(lines)
        chunk = "\n".join(lines[i:end]).strip()
        if chunk:
            out.append((num, chunk))
    return out


def _garbled(text):
    """A chunk the deterministic de-spacer could not fully repair — a high fraction of very short tokens
    (residual letter-spacing like 'devel o p m e n t', 'f r a m e w o rk'). The signal that the OPTIONAL
    LLM cleanup is worth a call."""
    toks = text.split()
    if len(toks) < 12:
        return False
    return sum(1 for t in toks if len(t) <= 2) / len(toks) > 0.33


def _llm_cleanup(text):
    """OPTIONAL, opt-in (SEGMENT_ENRICH=1): repair a garbled chunk's PROSE via one bounded broker
    /v1/complete call — rejoin residual letter-spaced / broken words ONLY, never add/remove/reword content
    (this is a citation surface). Best-effort: returns the input unchanged on any failure. NOTE: never send
    `temperature` to claude-sonnet-5 (deprecated → gateway 502s)."""
    prompt = ("Repair text extracted from a PDF. Rejoin letter-spaced and broken words into normal readable "
              "prose. Fix ONLY spacing and word-breaks — do NOT add, remove, summarise, reword, or reorder any "
              "content, and keep all numbers/section labels verbatim. Return only the corrected text.\n\n"
              + text[:2600])
    try:
        r = _call("/v1/complete", {"model": SEGMENT_MODEL, "max_tokens": 1400,
                                   "messages": [{"role": "user", "content": prompt}]}, timeout=58)
        out = (r.get("content") or r["choices"][0]["message"]["content"] or "").strip()
        return out or text
    except Exception as e:
        print(f"    [cleanup] failed ({e}) — keeping deterministic text"); return text


def chunk_eid(text, doc):
    """Content-anchored segmentation (ADR-0057). Each numbered unit -> a chunk with an eId locator, cleaned
    text, a heading (section-level), and any [Action:...] lifted to responsibility. Returns [(locator, text)]
    where locator is the rich dict written to knowledge.chunk.locator. Returns None if the doc has no usable
    numbering (caller falls back to the window chunker / LLM). With SEGMENT_ENRICH=1, chunks the deterministic
    de-spacer leaves garbled are polished by a bounded LLM call (prose-only; content preserved)."""
    units = _parse_units(text)
    numbered = [u for u in units if u[0] != "__preamble__"]
    if len(numbered) < 5:                                  # not a numbered document — let the caller fall back
        return None
    pieces = []
    for number, raw in units:
        if number == "__preamble__":
            pieces.append(({"kind": "eid", "eid": "front", "number": None,
                            "uri": _frbr_uri(doc, "front")}, _clean(raw)))
            continue
        actions = _ACTION.findall(raw)
        body = _ACTION.sub("", raw)
        clean = _clean(body)
        if SEGMENT_ENRICH and _garbled(clean):
            clean = _llm_cleanup(clean)
        eid = _eid_of(number)
        loc = {"kind": "eid", "eid": eid, "number": number,
               "path": ["__".join(_eid_of(number).split("__")[:i + 1]) for i in range(number.count(".") + 1)],
               "uri": _frbr_uri(doc, eid)}
        # a readable heading at EVERY level (from the clean text after the number) so the nav never shows a
        # raw eId — section headings are titles; paragraph "headings" are a short preview of their text
        htext = re.sub(r"^\s*" + re.escape(number) + r"\.?\s*", "", clean)
        loc["heading"] = re.sub(r"\s+\d+\s*$", "", htext).strip()[:70] or eid
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
            # SAME content — no new version. But REFRESH the manifest's locators in place (e.g. richer
            # headings) so downstream (the app's nav) sees the current metadata without a content change.
            if cur.get("chunks") != manifest:
                cur["chunks"] = manifest
                _call("/v1/write", {"scope": "knowledge.version", "id": cur["id"], "object": cur,
                                    "valid_time": {"from": now}, "asserted_at": now})
                print(f"  {did}: unchanged content (root {root[:22]}…) — refreshed manifest locators")
            else:
                print(f"  {did}: unchanged (root {root[:22]}…) — no new version")
            continue

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
