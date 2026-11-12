#!/usr/bin/env python3
"""Ward Works — public works and tenders on the map, by ward (writes works.ward_cell + procurement.ward_cell).

Neither a work order nor a tender carries coordinates; what they carry is a WARD NUMBER (works: the `ward` field of the
ward rollup; tenders: "…in Ward No-164 Vidyapeeta…" in the title). This engine joins those per-ward figures onto every
H3 cell of the ward (space.ward_cell, written by ward-h3-map from the uploaded ward boundary) so Visualize can colour the
city by ward:

  works.ward_cell        — the ward's works_count and paid_net_total (₹), from works.ward_rollup
  procurement.ward_cell  — the ward's tenders_count, from procurement.tender titles

Rules: a ward with no works / no tenders gets NO record for that layer (absence, not zero-filled); a tender whose title
names no ward number is counted as unplaced (reported), never guessed; only the run tenant's own records are used.
Ward numbers are normalised ("150", "150.0", "Ward 150" → "150"). Re-runs upsert (deterministic ids). Batched writes.

Env: RUN_TOKEN, AIRAWAT_API (the broker), TENANT (or WRITE_TENANT).
"""
import json, os, re, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/ward-works")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
BATCH, PAGE = 2000, 20000
_WARD_IN_TITLE = re.compile(r"\bward\s*(?:no\.?|number|num\.?)?\s*[-:#.]?\s*(\d{1,3})\b", re.I)


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]})
    return json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")


def read_all(schema, call=None):
    """Every record of a schema — pages past the 20,000-row query cap (cursor), and stops if the source repeats a page."""
    call = call or _call
    out, seen, cursor = [], set(), None
    for _ in range(50):
        body = {"scope": schema, "limit": PAGE}
        if cursor:
            body["cursor"] = cursor
        r = call("/v1/query", body)
        rows = [(x.get("object") or x) for x in (r.get("results") or [])]
        new = [o for o in rows if o.get("id") not in seen]
        seen.update(o.get("id") for o in new)
        out += new
        cursor = r.get("next_cursor")
        if not cursor or not new or len(rows) < PAGE:
            break
    return out


def ward_no(v):
    """'150' / 150 / 150.0 / 'Ward 150' / 'Ward No-150' → '150'; None when there is no number."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v))
    m = re.search(r"(\d{1,3})(?:\.0+)?\b", str(v))
    return str(int(m.group(1))) if m else None


def tender_ward(t):
    w = ward_no(t.get("ward")) if t.get("ward") not in (None, "") else None
    if w:
        return w
    m = _WARD_IN_TITLE.search(str(t.get("title") or ""))
    return str(int(m.group(1))) if m else None


def _mine(o, tenant):
    return str(o.get("id") or "").startswith("ds:%s:" % tenant)


def build(read, tenant, now):
    """→ (works rows, tender rows, stats). Pure: `read(schema)` returns that schema's records."""
    cells = {}                                                     # h3 → (ward_no, ward_name)
    for c in read("space.ward_cell"):
        if _mine(c, tenant) and c.get("h3_id"):
            w = ward_no(c.get("ward_no"))
            if w:
                cells[c["h3_id"]] = (w, c.get("ward_name"))
    centroid = {}
    for p in read("city.cell_profile"):
        ref = p.get("cell_ref") or {}
        if _mine(p, tenant) and ref.get("h3_id") and ref.get("centroid"):
            centroid[ref["h3_id"]] = ref["centroid"]
    works = {}
    for r in read("works.ward_rollup"):
        w = ward_no(r.get("ward"))
        if _mine(r, tenant) and w:
            works[w] = {"works_count": r.get("works_count"), "paid_net_total": r.get("paid_net_total")}
    tenders, unplaced = {}, 0
    for t in read("procurement.tender"):
        if not _mine(t, tenant):
            continue
        w = tender_ward(t)
        if w:
            tenders[w] = tenders.get(w, 0) + 1
        else:
            unplaced += 1
    prov = {"component": "%s@%s" % (CID, CVER), "method": "ward figures joined onto the ward's H3 cells (space.ward_cell)",
            "observed_at": now}
    wrows, trows = [], []
    for h, (w, name) in cells.items():
        ref = {"h3_id": h}
        if centroid.get(h):
            ref["centroid"] = centroid[h]
        base = {"tenant": tenant, "cell_ref": ref, "ward_no": w, "ward_name": name, "observed_at": now, "prov": prov}
        if w in works:
            wrows.append(dict(base, schema="works.ward_cell", id="ds:%s:works/ward_cell/%s" % (tenant, h),
                              **{k: v for k, v in works[w].items() if v is not None}))
        if w in tenders:
            trows.append(dict(base, schema="procurement.ward_cell", id="ds:%s:procurement/ward_cell/%s" % (tenant, h),
                              tenders_count=tenders[w]))
    stats = {"cells_with_ward": len(cells), "wards_with_works": len(works), "wards_with_tenders": len(tenders),
             "tenders_placed": sum(tenders.values()), "tenders_unplaced": unplaced,
             "works_wards_not_on_map": sorted(set(works) - {w for w, _ in cells.values()}, key=int)[:20]}
    return [{k: v for k, v in r.items() if v is not None} for r in wrows], \
           [{k: v for k, v in r.items() if v is not None} for r in trows], stats


def _write(rows, now):
    written = failed = 0
    for i in range(0, len(rows), BATCH):
        items = [{"scope": r["schema"], "id": r["id"], "object": r, "valid_time": {"from": now}} for r in rows[i:i + BATCH]]
        res = _call("/v1/write/batch", {"items": items, "asserted_at": now})
        written += int(res.get("written", 0)); failed += len(res.get("errors") or [])
    return written, failed


def main():
    if not TENANT:
        print("FAIL: TENANT not set — refusing to guess."); return 1
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def read(schema):
        try:
            return read_all(schema)
        except Exception as e:
            print("  %s: not read (%s)" % (schema, str(e)[:120]), flush=True); return []

    wrows, trows, stats = build(read, TENANT, now)
    print("tenant=%s · %s" % (TENANT, json.dumps(stats)), flush=True)
    if not stats["cells_with_ward"]:
        print("no space.ward_cell yet — upload the ward boundary (ward-ingest) and run ward-h3-map first. Nothing written.")
        return 0
    ww, wf = _write(wrows, now)
    tw, tf = _write(trows, now)
    print("RESULT: " + json.dumps({"works_cells": ww, "tender_cells": tw, "failed": wf + tf, **stats}))
    return 0 if not (wf or tf) else 1


if __name__ == "__main__":
    raise SystemExit(main())
