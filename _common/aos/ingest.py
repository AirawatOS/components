"""aos.ingest — the generic "tabular file → governed records, idempotently" primitive.

Every CSV/table ingest driver was hand-writing the SAME ~150 lines: fetch the blob, parse the CSV, resolve
columns from the header (with env overrides), parse dates, derive a stable id, preload what's already stored and
write ONLY the new + changed rows, tally + log. That is platform plumbing, not domain logic. `ingest.table()` owns
all of it; a driver supplies only its per-row mapping to the governed envelope.

ADDITIVE + decoupled: this module touches nothing else in the SDK. A driver stays a normal `@driver.main` that
calls `ingest.table(...)`; `driver`/`ctx` are unchanged, so no existing component is affected.

    from aos import driver, ctx, ingest
    cat = ingest.crosswalk(canonical=CANON, env="TAXONOMY_MAP", keywords=KEYWORDS)
    def map_row(r):
        reg = ingest.date(r.get("reg_date"), r.get("reg_time"))
        if not reg: return None                       # None ⇒ skip this row (e.g. a required field is missing)
        return {"category": cat(r.get("category")), "status": r.get("status") or "unknown",
                "registered_at": reg, "reopens": ingest.int_(r.get("reopens"))}
    @driver.main
    def run():
        return ingest.table(schema="water.complaint", map_row=map_row, columns=CANDIDATES,
                            key_field="tracking_no", key_fields=("raw_category","registered_at","source_channel"))
"""
import os, io, csv, re, json, hashlib
from . import ctx as _ctx


# ── scalar helpers (lifted verbatim from the hand-rolled drivers; identical semantics) ─────────────────────────────
_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?)?", re.I)

def date(date_s, time_s=""):
    """Tolerant DD/MM/YYYY[ HH:MM:SS am/pm] (or already-ISO) → 'YYYY-MM-DDTHH:MM:SS'. Unparseable → None (NEVER guessed).
    `time_s` (a separate time column) fills the clock when the date field carries no time."""
    s = (date_s or "").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s.replace(" ", "T")[:19]
    m = _DATE.match(s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    hh, mm, ss, ap = m.group(4), m.group(5), m.group(6), (m.group(7) or "").lower()
    if hh is None and time_s:
        tm = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?", str(time_s).strip(), re.I)
        if tm:
            hh, mm, ss, ap = tm.group(1), tm.group(2), tm.group(3), (tm.group(4) or "").lower()
    hh = int(hh) if hh is not None else 0
    mm = int(mm) if mm is not None else 0
    ss = int(ss) if ss is not None else 0
    if ap == "pm" and hh < 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"


def int_(v):
    """Best-effort int ('3', '3.0', ' 3 ') → 3; blank/garbage → None (never fabricated)."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def jsonenv(name):
    """Read a JSON object from an env var (e.g. ADMIN_COLUMNS). Invalid/absent → {} (logged once)."""
    cfg = os.environ.get(name)
    if not cfg:
        return {}
    try:
        v = json.loads(cfg)
        return v if isinstance(v, dict) else {}
    except Exception:
        print(f"[ingest] {name} is not valid JSON — ignoring", flush=True)
        return {}


def crosswalk(*, canonical=None, env=None, keywords=None, enum=None):
    """Return a classify(raw_label) → canonical-category callable. Resolution order: explicit map (TAXONOMY_MAP-style
    env JSON, raw-label-lowercased → canonical) first, then an ordered keyword classifier, then 'other'. The board's
    ORIGINAL label is the driver's to preserve (keep it in a raw_* field) — this only derives the canonical bucket.
    `canonical` is the authoritative allowed set (targets outside it are dropped). `enum` is reserved (S2: read the
    allowed set from the governed schema so it can't drift) — not wired in S1; pass `canonical` explicitly."""
    canon = list(canonical or [])
    canon_set = set(canon)
    tmap = {}
    if env:
        cfg = os.environ.get(env)
        if cfg:
            try:
                m = {str(k).lower().strip(): str(v).strip() for k, v in json.loads(cfg).items()}
                bad = {v for v in m.values() if canon_set and v not in canon_set}
                if bad:
                    print(f"[ingest] {env} targets not in canonical taxonomy (ignored): {sorted(bad)}", flush=True)
                tmap = {k: v for k, v in m.items() if (not canon_set or v in canon_set)}
            except Exception:
                print(f"[ingest] {env} is not valid JSON — using keyword classifier", flush=True)
    kws = keywords or []
    fallback = "other" if (not canon_set or "other" in canon_set) else (canon[-1] if canon else "other")

    def classify(raw_label):
        s = (raw_label or "").lower().strip()
        if not s:
            return fallback
        if s in tmap:
            return tmap[s]
        for cat, ks in kws:
            if any(k in s for k in ks):
                return cat
        return fallback
    return classify


def _crosswalk_prompt(labels, canonical, hint=""):
    """The (shared) prompt that maps a board's DISTINCT labels onto the canonical categories. One call for the whole
    vocabulary — NOT per row. Kept identical to the app_host /bridge/suggest-crosswalk prompt (this is the canonical copy)."""
    cats = ", ".join(canonical)
    lab = "\n".join(f"{i+1}. {l}" for i, l in enumerate(labels))
    return (f"You map a source system's complaint/category labels onto a FIXED set of canonical categories.\n"
            + (f"Domain context: {hint}\n" if hint else "")
            + f"Canonical categories — choose EXACTLY ONE per label (the closest fit; only use \"other\" if truly none fit):\n  {cats}\n\n"
            + f"Labels to map:\n{lab}\n\n"
            + "Return ONLY a JSON object mapping EACH label (verbatim) to one canonical category, e.g. "
            + "{\"No water\": \"no_water\"}. No prose, no code fences, every label present as a key.")


def _parse_crosswalk(text, labels, canonical):
    """Parse the model's reply into {label: canonical} — keeping ONLY labels present with a valid canonical target.
    Everything else goes to `unmapped` (the human fills those in). Never fabricates a label the model didn't return."""
    canon_set = set(canonical)
    out, unmapped = {}, []
    try:
        i, j = text.index("{"), text.rindex("}")
        m = json.loads(text[i:j + 1])
    except Exception:
        m = {}
    low = {str(k).strip().lower(): k for k in m}
    for lab in labels:
        v = m.get(lab)
        if v is None:
            v = m.get(low.get(lab.strip().lower(), ""))        # case-insensitive match on the returned key
        v = str(v).strip() if v is not None else ""
        if v in canon_set:
            out[lab] = v
        else:
            unmapped.append(lab)
    return {"map": out, "unmapped": unmapped}


def suggest_crosswalk(labels, canonical, *, hint="", ctx=None, ai=None, max_tokens=1200):
    """Use the confined model (kimi-k3 via ctx.ai) to PROPOSE a taxonomy crosswalk for a board's DISTINCT labels →
    canonical categories — ONCE, over the vocabulary (tens of labels), NOT per row (idempotency- and cost-safe). The
    result is a SUGGESTION a human reviews before it becomes config (TAXONOMY_MAP); it never touches the ingest hot
    path. `ai` lets a caller inject the completion fn (tests); otherwise ctx.ai / the module ctx is used.
    Returns {"map": {label: canonical}, "unmapped": [labels the model didn't map — fill these in by hand]}."""
    labels = [str(l).strip() for l in labels if str(l).strip()]
    if not labels:
        return {"map": {}, "unmapped": []}
    _ai = ai or (ctx or _ctx).ai
    prompt = _crosswalk_prompt(labels, canonical, hint)
    try:
        text = _ai([{"role": "user", "content": prompt}], max_tokens)
        text = text if isinstance(text, str) else (text or {}).get("content", "")
    except Exception as e:
        print(f"[ingest.suggest_crosswalk] model call failed: {e}", flush=True)
        return {"map": {}, "unmapped": labels}
    return _parse_crosswalk(text or "", labels, canonical)


class Row:
    """A header-resolved, case-insensitive row accessor passed to map_row.
      r.get("category")  → value of the logical field (resolved via the `columns` spec), stripped
      r.raw("Ward")      → value by actual header name (case-insensitive), stripped
      r.pick({"ward":"Ward","zone":"Zone"}) → {out: value} for non-empty values (admin hierarchies etc.)"""
    __slots__ = ("_raw", "_cols", "_lower")

    def __init__(self, raw, cols, lower):
        self._raw, self._cols, self._lower = raw, cols, lower

    def get(self, logical):
        c = self._cols.get(logical)
        return (self._raw.get(c) or "").strip() if c else ""

    def raw(self, header_name):
        h = self._lower.get(str(header_name).lower().strip())
        return (self._raw.get(h) or "").strip() if h else ""

    def pick(self, mapping):
        out = {}
        for k, hn in (mapping or {}).items():
            v = self.raw(hn)
            if v:
                out[k] = v
        return out


def _resolve_columns(header, spec):
    """logical field → actual header (case-insensitive), honouring a candidate list (first match wins).
    A candidate of None (an unset env override) is skipped."""
    lower = {(h or "").lower().strip(): h for h in header}
    out = {}
    for field, cands in (spec or {}).items():
        for c in cands:
            if c and str(c).lower() in lower:
                out[field] = lower[str(c).lower()]
                break
    return out


def _sig(obj, exclude):
    return json.dumps({k: v for k, v in obj.items() if k not in exclude}, sort_keys=True, ensure_ascii=False)


def table(*, schema, map_row, columns=None, file_env="FILE_ID", source_env="SOURCE", max_env="MAX",
          key_field=None, key_fields=None, sig_exclude=("id", "tenant"), tally_field=None, ctx=None):
    """Ingest a CSV blob (filestore id in `os.environ[file_env]`) into `schema`, idempotently.

    map_row(Row) → a governed-envelope dict (without id/tenant — they're stamped here) or None to SKIP the row.
    - columns: {logical: [candidate headers…]} (include os.environ.get("X_FIELD") as the first candidate for an override).
    - id: `ds:{tenant}:{schema-as-path}/{key}` where key = the sanitized value of `key_field` if present, else a
      content hash of `key_fields` (STABLE identity fields — NOT status/closure, so a later update collapses onto the
      same id instead of duplicating). If map_row sets "id", that wins.
    - idempotency: preloads stored rows once; writes only NEW + CHANGED (change = any field outside sig_exclude moved);
      SKIP_UNCHANGED=0 forces a full rewrite. Returns {"new","changed","unchanged","skipped","tally"}.
    """
    cx = ctx or _ctx
    tenant = cx.tenant
    if not tenant:
        print(f"[{schema}] no tenant in this context — nothing to do", flush=True)
        return {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "tally": {}}
    file_id = os.environ.get(file_env, "")
    if not file_id:
        print(f"[{schema}] {file_env} not set — nothing to ingest", flush=True)
        return {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "tally": {}}
    max_n = int(os.environ.get(max_env, "0") or 0)
    skip_unchanged = os.environ.get("SKIP_UNCHANGED", "1") not in ("0", "false", "False")
    id_prefix = schema.replace(".", "/")
    source = os.environ.get(source_env) or None

    existing = {}
    if skip_unchanged:
        try:
            for r in cx.facts(schema).list():
                o = r.get("object") or r
                if o.get("id"):
                    existing[o["id"]] = _sig(o, sig_exclude)
        except Exception as e:
            print(f"[{schema}] could not preload existing (treating all as new): {e}", flush=True)

    raw = cx.file.get(file_id)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    rdr = csv.DictReader(io.StringIO(text))
    header = rdr.fieldnames or []
    cols = _resolve_columns(header, columns or {})
    lower = {(h or "").lower().strip(): h for h in header}

    new = changed = unchanged = skipped = 0
    tally = {}
    for raw_row in rdr:
        obj = map_row(Row(raw_row, cols, lower))
        if not obj:
            skipped += 1
            continue
        obj.setdefault("schema", schema)
        if source and "source" not in obj:
            obj["source"] = source
        if not obj.get("id"):
            kv = obj.get(key_field) if key_field else None
            if kv:
                key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(kv))
            else:
                sig = "|".join(str(obj.get(f, "")) for f in (key_fields or sorted(obj.keys())))
                key = "h" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]
            obj["id"] = f"ds:{tenant}:{id_prefix}/{key}"
        obj.setdefault("tenant", tenant)
        obj = {k: v for k, v in obj.items() if v is not None}
        s = _sig(obj, sig_exclude)
        if skip_unchanged and obj["id"] in existing:
            if existing[obj["id"]] == s:
                unchanged += 1
                continue
            changed += 1
        else:
            new += 1
        cx.write(obj)
        if tally_field and obj.get(tally_field) is not None:
            tally[obj[tally_field]] = tally.get(obj[tally_field], 0) + 1
        if max_n and (new + changed) >= max_n:
            break
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:6]
    print(f"[{schema}] wrote {new} new + {changed} changed; skipped {unchanged} unchanged + {skipped} rows "
          f"(dedup by stable id)" + (f"; {tally_field}: {top}" if tally_field else ""), flush=True)
    return {"new": new, "changed": changed, "unchanged": unchanged, "skipped": skipped, "tally": tally}
