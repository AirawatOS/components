"""procurement-tenders — generic public-procurement ingest (ADR-0110).

Reads a procurement feed (NDJSON or CSV) from a configurable source URL via the broker, filters by
procuring entity + topic, maps each record → procurement.tender@0.2.0, and writes to the tenant.

AGENCY-NEUTRAL: no portal/agency/city is hardcoded. The source URL, entity filter, and topic filter
are config. For BBMP's first instance the upstream is the Karnataka e-procurement / KPPP data as
republished (TenderSir); a different city points SOURCE_URL at its own export. Columnar Parquet sources
are converted to NDJSON by a source adapter until a pyarrow runtime profile is available (see meta).

Config (env / --set):
  SOURCE_FILE_ID  a filestore id (content-addressed) holding the NDJSON/CSV feed — the clean in-platform
                  delivery path (digest-verified, no egress). Takes precedence over SOURCE_URL.
  SOURCE_URL      the NDJSON (one JSON object per line) or CSV feed URL (external egress alternative)
  SOURCE_FORMAT   ndjson | csv                                                   [default: ndjson]
  SOURCE_REF      ref/source id for attribution                                  [default: generic]
  ENTITY_FILTER   case-insensitive substring an incoming record's entity/dept/location must contain
                  to be kept (e.g. the agency name). Empty = keep all.           [default: ""]
  TOPIC_FILTER    case-insensitive substring any of the record's topics must contain (e.g. "road").
                  Empty = keep all.                                              [default: ""]
  MAX_ROWS        safety cap on rows written                                     [default: 200000]
"""
import os, io, csv, json
from aos import driver, ctx, egress, now

WRITE_CHUNK = 800

# Incoming field aliases (any one of these in a source row maps to our generic field). Case-insensitive.
A = {
    "tender_ref":      ["tenderNumber", "tender_ref", "tender_no", "tender_number"],
    "source":          ["source"],
    "procuring_entity":["department", "procuring_entity", "organisation", "organization", "dept"],
    "jurisdiction_ref":["location", "jurisdiction", "place"],
    "title":           ["title", "work_name", "name_of_work"],
    "description":     ["description", "desc"],
    "category":        ["category"],
    "sub_category":    ["subCategory", "sub_category"],
    "status":          ["statusLabel", "status"],
    "invite_type":     ["inviteType", "invite_type"],
    "eval_type":       ["evalType", "evaluationType", "eval_type"],
    "estimated_value": ["estimatedValue", "estimated_value", "value", "estimate"],
    "accepted_value":  ["accepted_value", "awardValue", "awarded_value"],
    "tender_fee":      ["tenderFee", "tender_fee", "fee"],
    "emd":             ["emd", "earnestMoney"],
    "bid_validity_days":["bidValidityPeriod", "bid_validity_days"],
    "no_of_calls":     ["noOfCalls", "no_of_calls", "call"],
    "contractor_class":["contractor_class", "class"],
    "published_at":    ["publishedDate", "published_at", "published"],
    "closing_at":      ["closingDate", "closing_at", "closing"],
    "awarded_at":      ["awardedDate", "awarded_at", "awarded"],
    "period_year":     ["year", "period_year"],
    "num_bidders":     ["numBidders", "num_bidders"],
}
# JSON-encoded array fields (strings in the feed)
AJ = {
    "topics":    ["topicsJson", "topics"],
    "bidders":   ["biddersJson", "bidders"],
    "documents": ["documentsJson", "documents"],
    "corrigenda":["corrigendumsJson", "corrigenda"],
}

def _pick(row_lc, names):
    for n in names:
        v = row_lc.get(n.lower())
        if v not in (None, ""):
            return v
    return None

def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None

def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except Exception:
        return None

def _arr(v):
    """Parse a JSON-array field that arrives as a string (or already a list)."""
    if v in (None, ""):
        return None
    if isinstance(v, list):
        return v
    try:
        p = json.loads(v)
        return p if isinstance(p, list) else [p]
    except Exception:
        return [v]

def _docs(v):
    """Normalise a documents array → [{file_name, doc_type, uuid}]."""
    out = []
    for d in (_arr(v) or []):
        if isinstance(d, dict):
            out.append({"file_name": d.get("fileName") or d.get("file_name"),
                        "doc_type": d.get("documentType") or d.get("doc_type"),
                        "uuid": d.get("uuid")})
    return out or None

def _slug(s):
    return "".join(c if (c.isalnum()) else "-" for c in (s or "").lower()).strip("-")[:48] or "x"

def map_tender(row, tenant, source_ref):
    """Pure mapping: one source row (dict) → one procurement.tender@0.2.0 object (or None)."""
    lc = {str(k).lower(): v for k, v in row.items()}
    ref = _pick(lc, A["tender_ref"])
    if not ref:
        return None
    topics = _arr(_pick(lc, AJ["topics"]))
    o = {
        "schema": "procurement.tender",
        "id": f"ds:{tenant}:procurement/tender/{_slug(ref)}",
        "tenant": tenant,
        "tender_ref": str(ref),
        "source": _pick(lc, A["source"]) or source_ref,
        "procuring_entity": _pick(lc, A["procuring_entity"]),
        "jurisdiction_ref": _pick(lc, A["jurisdiction_ref"]),
        "title": _pick(lc, A["title"]),
        "description": _pick(lc, A["description"]),
        "category": _pick(lc, A["category"]),
        "sub_category": _pick(lc, A["sub_category"]),
        "topics": topics,
        "status": _pick(lc, A["status"]),
        "invite_type": _pick(lc, A["invite_type"]),
        "eval_type": _pick(lc, A["eval_type"]),
        "estimated_value": _num(_pick(lc, A["estimated_value"])),
        "accepted_value": _num(_pick(lc, A["accepted_value"])),
        "tender_fee": _num(_pick(lc, A["tender_fee"])),
        "emd": _num(_pick(lc, A["emd"])),
        "bid_validity_days": _int(_pick(lc, A["bid_validity_days"])),
        "no_of_calls": _int(_pick(lc, A["no_of_calls"])),
        "contractor_class": _pick(lc, A["contractor_class"]),
        "published_at": _pick(lc, A["published_at"]),
        "closing_at": _pick(lc, A["closing_at"]),
        "awarded_at": _pick(lc, A["awarded_at"]),
        "period_year": (str(_pick(lc, A["period_year"])) if _pick(lc, A["period_year"]) is not None else None),
        "bidders": _arr(_pick(lc, AJ["bidders"])),
        "num_bidders": _int(_pick(lc, A["num_bidders"])),
        "documents": _docs(_pick(lc, AJ["documents"])),
        "corrigenda": _docs(_pick(lc, AJ["corrigenda"])),
        "prov": {"source": source_ref, "component": "procurement-tenders@0.1.0"},
    }
    return o

def _keep(o, entity_filter, topic_filter):
    if entity_filter:
        hay = " ".join(str(o.get(k) or "") for k in ("procuring_entity", "jurisdiction_ref", "title")).lower()
        if entity_filter.lower() not in hay:
            return False
    if topic_filter:
        tops = [str(t).lower() for t in (o.get("topics") or [])]
        title = str(o.get("title") or "").lower()
        if not any(topic_filter.lower() in t for t in tops) and topic_filter.lower() not in title:
            return False
    return True

def _rows(body, fmt):
    if fmt == "csv":
        for r in csv.DictReader(io.StringIO(body)):
            yield r
    else:  # ndjson
        for line in body.splitlines():
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    continue

@driver.main
def run():
    T = ctx.tenant
    file_id = (os.environ.get("SOURCE_FILE_ID") or "").strip()
    url = os.environ.get("SOURCE_URL")
    if not file_id and not url:
        raise SystemExit("SOURCE_FILE_ID or SOURCE_URL is required")
    fmt = (os.environ.get("SOURCE_FORMAT") or "ndjson").strip().lower()
    source_ref = os.environ.get("SOURCE_REF") or "procurement-feed"
    entity_filter = (os.environ.get("ENTITY_FILTER") or "").strip()
    topic_filter = (os.environ.get("TOPIC_FILTER") or "").strip()
    max_rows = int(os.environ.get("MAX_ROWS") or "200000")

    src = f"file:{file_id}" if file_id else url
    print(f"[procurement-tenders] tenant={T} src={src} fmt={fmt} entity={entity_filter!r} topic={topic_filter!r}", flush=True)
    if file_id:
        raw = ctx.file.get(file_id)                       # content-addressed, digest-verified, no egress
        body = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    else:
        r = ctx.fetch(url)
        if not getattr(r, "ok", False):
            raise SystemExit(f"fetch failed: {getattr(r,'status','?')}")
        body = r.body if isinstance(r.body, str) else r.body.decode("utf-8", "replace")

    buf, written, seen = [], 0, 0
    for row in _rows(body, fmt):
        seen += 1
        o = map_tender(row, T, source_ref)
        if not o or not _keep(o, entity_filter, topic_filter):
            continue
        buf.append(o)
        if len(buf) >= WRITE_CHUNK:
            ctx.write_many(buf); written += len(buf); buf = []
            print(f"  …{written} written", flush=True)
            if written >= max_rows:
                break
    if buf and written < max_rows:
        ctx.write_many(buf); written += len(buf)
    print(f"[procurement-tenders] seen={seen} written={written}", flush=True)

if __name__ == "__main__":
    driver.run()
