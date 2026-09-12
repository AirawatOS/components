#!/usr/bin/env python3
"""news-geocode — put civic.issue on the MAP.

A civic.issue carries a jurisdiction NAME + a headline/summary but no coordinates. This engine:
  1. Asks the model gateway (/v1/complete) to extract, for each issue, the SPECIFIC locality named in the text —
     a neighbourhood / colony / road / landmark WITHIN the city — or null when the text names no sub-city place.
  2. Geocodes that locality with a REAL geocoder (Nominatim), egress-gated through the broker, bounded to the city
     AOI (VIEWBOX) + country, and disambiguated with CITY_HINT.
  3. Writes the resolved point back onto the civic.issue as `geometry` (GeoJSON Point) + `place_label` +
     `geocode_confidence` + `geocoded_at`.

NO fabrication: an issue whose text names no locality, or whose locality the geocoder cannot resolve INSIDE the AOI,
is left WITHOUT geometry — never given the city centroid or a guessed point. Rate-limited (Nominatim policy) + capped
(MAX) + RESUMES (skips issues that already carry geometry, unless RECODE=1).
"""
import os, json, re, time, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
GEOCODER = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
CITY_HINT = os.environ.get("CITY_HINT", "")                     # e.g. "New Delhi, India" — disambiguates the locality
VIEWBOX = os.environ.get("VIEWBOX", "")                         # "minlon,minlat,maxlon,maxlat" AOI — rejects strays
MAX = int(os.environ.get("MAX", "80"))                          # cap per run (public geocoder is rate-limited)
BATCH = int(os.environ.get("BATCH", "10"))
SLEEP = float(os.environ.get("GEOCODE_SLEEP", "1.1"))          # >=1s for Nominatim usage policy
RECODE = os.environ.get("RECODE", "") not in ("", "0", "false")  # re-geocode issues that already have geometry
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
METHOD = "news-geocode@0.1.0"


def _call(path, body, timeout=120):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")


def query(scope):
    return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]


def write(scope, oid, obj):
    obj = {k: v for k, v in obj.items() if v is not None}
    _call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def unwrap(res):
    if isinstance(res, dict):
        for k in ("body", "data", "result", "response", "content"):
            if k in res:
                return res[k]
    return res


def _parse_json_val(v):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def _json_array(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def extract_places(items):
    """One /v1/complete call → {index: locality_string_or_None}. The model returns the most specific place NAMED in
    the text that lies WITHIN the city; null when the item names no sub-city locality (so it gets no pin)."""
    city = CITY_HINT or "the city"
    lines = "\n".join(f"{i}. {(it.get('title') or '')} — {(it.get('summary') or '')[:220]}"
                      for i, it in enumerate(items))
    prompt = (
        f"You geolocate civic news within {city}. For EACH numbered item, identify the MOST SPECIFIC real place it is "
        f"about that lies INSIDE {city} — a neighbourhood, colony, locality, road, market, or named landmark.\n"
        "Rules:\n"
        f"- Return the place as a short string suitable for a geocoder (e.g. \"Karol Bagh\", \"Ring Road, Lajpat Nagar\").\n"
        "- Return null for `place` when the item names NO specific sub-city locality (e.g. it is about the whole "
        "city, the state, national politics, or a general topic). DO NOT guess a place.\n"
        "For EACH item return an object: {index (int), place (string or null)}.\n"
        "Return ONLY a JSON array, no prose.\n\nITEMS:\n" + lines)
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}], "max_tokens": 1500})
    except Exception as e:
        raise SystemExit(f"FATAL: /v1/complete unavailable ({e}) — cannot extract localities (no synthetic fallback).")
    out = {}
    for r in _json_array(res.get("content", "")):
        if isinstance(r, dict) and "index" in r:
            p = r.get("place")
            out[int(r["index"])] = (p.strip() if isinstance(p, str) and p.strip().lower() not in ("null", "none", "") else None)
    return out


def geocode(place):
    """locality → (lon, lat, confidence) via the geocoder inside the AOI, or (None,None,0.0) if UNRESOLVED."""
    q = place + ((", " + CITY_HINT) if CITY_HINT else "")
    params = {"q": q, "format": "json", "limit": 1, "addressdetails": 0, "countrycodes": "in"}
    if VIEWBOX:
        params["viewbox"] = VIEWBOX; params["bounded"] = 1     # hard-clip to the city AOI — reject out-of-city hits
    url = GEOCODER + "?" + urllib.parse.urlencode(params)
    try:
        hits = _parse_json_val(unwrap(_call("/v1/fetch", {"url": url}))) or []
    except Exception as e:
        print(f"[geocode] fetch error for {place!r}: {e}", flush=True); return None, None, 0.0
    if not hits:
        return None, None, 0.0
    h = hits[0]
    try:
        return float(h["lon"]), float(h["lat"]), round(float(h.get("importance", 0.5)), 3)
    except Exception:
        return None, None, 0.0


def main():
    if not TOKEN or not TENANT:
        print("news-geocode — handlers: extract_places, geocode (dev self-test, no tenant/token).")
        return 0
    issues = [i for i in query("civic.issue") if (i.get("tenant") == TENANT or not i.get("tenant"))]
    todo = [i for i in issues if RECODE or not i.get("geometry")][:MAX]
    print(f"[news-geocode] {len(issues)} civic.issue · {len(todo)} to geocode (cap {MAX}) · viewbox={'yes' if VIEWBOX else 'no'}", flush=True)
    geocoded = no_place = unresolved = 0
    for b in range(0, len(todo), BATCH):
        batch = todo[b:b + BATCH]
        places = extract_places(batch)
        for i, it in enumerate(batch):
            place = places.get(i)
            if not place:
                no_place += 1; continue
            lon, lat, conf = geocode(place)
            time.sleep(SLEEP)
            if lon is None:
                unresolved += 1; continue
            upd = {**it, "geometry": {"type": "Point", "coordinates": [lon, lat]},
                   "place_label": place, "geocode_confidence": conf, "geocoded_at": NOW, "geocode_method": METHOD}
            write("civic.issue", it["id"], upd)
            geocoded += 1
        print(f"  processed {min(b+BATCH,len(todo))}/{len(todo)} · geocoded {geocoded}", flush=True)
    print(f"[news-geocode] geocoded {geocoded} · no-locality {no_place} · unresolved {unresolved} "
          f"(the latter two left WITHOUT a point — not fabricated)", flush=True)
    print("RESULT: " + json.dumps({"geocoded": geocoded, "no_place": no_place, "unresolved": unresolved}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
