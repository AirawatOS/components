#!/usr/bin/env python3
"""Ward Works — public works and tenders on the map, by ward (writes works.ward_cell + procurement.ward_cell).

Neither a work order nor a tender carries coordinates; what they carry is a WARD NUMBER (works: the `ward` field of the
ward rollup; tenders: "…in Ward No-164 Vidyapeeta…" in the title). This engine joins those per-ward figures onto every
H3 cell of the ward (space.ward_cell, written by ward-h3-map from the uploaded ward boundary) so Visualize can colour the
city by ward:

  works.ward_cell        — the ward's works_count and paid_net_total (₹), from works.ward_rollup, plus (0.2.0) a
                           yearly `series` by work-order year (YEAR_FROM–YEAR_TO) that Visualize animates
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
_WARD_IN_TITLE = re.compile(r"\bward\s*(?:no\.?|number|num\.?)?\s*[-:#.]?\s*(\d{1,3})\b[\s,.-]*([A-Za-z][A-Za-z .]{2,30})?", re.I)
# BBMP renumbered its wards (198 → 243) in 2022: tenders up to 2021 always use the 2010 numbering; from 2022 on many use
# the new one (staging: 981 titles name a 2022 ward, 644 a number above 198). The map's boundaries are the 2010 wards.
NEW_NUMBERING_FROM = int(os.environ.get("NEW_NUMBERING_FROM", "2022"))
# 0.2.0 — a YEARLY series per ward (by work-order year) so Visualize can animate spend over the years. The register's
# payments carry no dates, so a work's net payment is attributed to the year its work order was issued.
YEAR_FROM = int(os.environ.get("YEAR_FROM", "2012"))
YEAR_TO = int(os.environ.get("YEAR_TO", "2022"))


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


def _norm(s):
    return re.sub(r"[^a-z]", "", str(s or "").lower().replace("ward", ""))


def _year(t):
    for k in ("published_at", "closing_at", "awarded_at"):
        m = re.match(r"(\d{4})", str(t.get(k) or ""))
        if m:
            return int(m.group(1))
    return None


def tender_ward(t, names=None):
    """→ (ward_no, reason). The ward number from the `ward` field or the title, accepted only when it is a ward ON THE MAP
    and — for a tender from NEW_NUMBERING_FROM on, when BBMP's 243-ward numbering was in use — only when the ward name in
    the title matches the mapped ward of that number. Anything else is unplaced, with the reason; never guessed."""
    names = names or {}
    w = ward_no(t.get("ward")) if t.get("ward") not in (None, "") else None
    nm = ""
    if not w:
        m = _WARD_IN_TITLE.search(str(t.get("title") or ""))
        if not m:
            return None, "no ward in title"
        w, nm = str(int(m.group(1))), _norm(m.group(2))
    if names and w not in names:
        return None, "not a ward on the map (newer numbering)"
    y = _year(t)
    if names and y is not None and y >= NEW_NUMBERING_FROM:
        mapped = _norm(names.get(w))
        if not (len(nm) >= 4 and mapped and nm[:5] == mapped[:5]):
            return None, "%d+ tender, ward name does not confirm the 2010 numbering" % NEW_NUMBERING_FROM
    return w, None


def order_year(wo):
    """Year of a work order from work_order_date ('2015-04-01', '01/04/2015', '01-Apr-15'…) or sanctioned_year ('2023-24')."""
    for k in ("work_order_date", "sanctioned_year"):
        v = str(wo.get(k) or "")
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", v)
        if m:
            return int(m.group(1))
        m = re.match(r"^\d{1,2}[-/][A-Za-z]{3}[-/](\d{2})$", v)
        if m:
            return 2000 + int(m.group(1))
    return None


def yearly(read, tenant):
    """→ ({ward: {year: {"works_count", "paid_net_total"}}}, undated). Payments are joined to their work order."""
    paid = {}
    for p in read("works.payment"):
        if _mine(p, tenant) and p.get("work_order") and isinstance(p.get("paid_net"), (int, float)):
            paid[p["work_order"]] = paid.get(p["work_order"], 0.0) + p["paid_net"]
    by, undated = {}, 0
    for wo in read("works.work_order"):
        if not _mine(wo, tenant):
            continue
        w, y = ward_no(wo.get("ward")), order_year(wo)
        if not w:
            continue
        if y is None:
            undated += 1; continue
        if not (YEAR_FROM <= y <= YEAR_TO):
            continue
        cell = by.setdefault(w, {}).setdefault(y, {"works_count": 0, "paid_net_total": 0.0})
        cell["works_count"] += 1
        cell["paid_net_total"] += paid.get(wo.get("id"), 0.0)
    return by, undated


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
    by_year, undated = yearly(read, tenant)
    years = list(range(YEAR_FROM, YEAR_TO + 1))
    works = {}
    for r in read("works.ward_rollup"):
        w = ward_no(r.get("ward"))
        if _mine(r, tenant) and w:
            works[w] = {"works_count": r.get("works_count"), "paid_net_total": r.get("paid_net_total")}
    names = {}
    for w, nm in cells.values():
        names.setdefault(w, nm)
    tenders, unplaced, why = {}, 0, {}
    for t in read("procurement.tender"):
        if not _mine(t, tenant):
            continue
        w, reason = tender_ward(t, names)
        if w:
            tenders[w] = tenders.get(w, 0) + 1
        else:
            unplaced += 1; why[reason] = why.get(reason, 0) + 1
    prov = {"component": "%s@%s" % (CID, CVER), "method": "ward figures joined onto the ward's H3 cells (space.ward_cell)",
            "observed_at": now}
    wrows, trows = [], []
    for h, (w, name) in cells.items():
        ref = {"h3_id": h}
        if centroid.get(h):
            ref["centroid"] = centroid[h]
        base = {"tenant": tenant, "cell_ref": ref, "ward_no": w, "ward_name": name, "observed_at": now, "prov": prov}
        if w in works:
            row = dict(base, schema="works.ward_cell", id="ds:%s:works/ward_cell/%s" % (tenant, h),
                       **{k: v for k, v in works[w].items() if v is not None})
            if w in by_year:                      # every year in the window: a year with no work orders IS zero for the ward
                row["series"] = [{"t": str(y), "works_count": by_year[w].get(y, {}).get("works_count", 0),
                                  "paid_net_total": round(by_year[w].get(y, {}).get("paid_net_total", 0.0), 2)} for y in years]
            wrows.append(row)
        if w in tenders:
            trows.append(dict(base, schema="procurement.ward_cell", id="ds:%s:procurement/ward_cell/%s" % (tenant, h),
                              tenders_count=tenders[w]))
    stats = {"cells_with_ward": len(cells), "wards_with_works": len(works), "wards_with_tenders": len(tenders),
             "tenders_placed": sum(tenders.values()), "tenders_unplaced": unplaced, "unplaced_by_reason": why,
             "works_wards_not_on_map": sorted(set(works) - {w for w, _ in cells.values()}, key=int)[:20],
             "series_years": "%d-%d" % (YEAR_FROM, YEAR_TO), "wards_with_series": len(by_year), "work_orders_undated": undated}
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
