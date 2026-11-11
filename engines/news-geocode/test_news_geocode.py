#!/usr/bin/env python3
"""Behavioural check — News Geocode 0.2.0 places civic issues from the tenant's OWN gazetteer first.

Drives the real main() with stubbed registry / model / geocoder calls:
  • a locality the gazetteer knows ("Indiranagar", also "Indiranagar Area") is placed from space.place — the public
    geocoder is NOT called;
  • a locality the gazetteer lacks falls back to the geocoder, bounded by a VIEWBOX derived from the tenant AOI and a
    CITY_HINT from the AOI name ("Bengaluru, India") — no hand-set config;
  • an issue with no locality, or one the geocoder cannot resolve, gets NO point (never the city centroid);
  • another tenant's gazetteer row is ignored; a near-miss name is never fuzzily matched.
Exit 1 on failure.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.update({"RUN_TOKEN": "t", "TENANT": "janaagraha", "CITY_HINT": "", "VIEWBOX": "", "GEOCODE_SLEEP": "0"})
import run as ng   # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok:
        fails.append(name)


DATA = {
    "space.aoi": [{"id": "ds:janaagraha:space/aoi/bengaluru", "bbox": [77.46, 12.83, 77.78, 13.14]}],
    "space.place": [
        {"tenant": "janaagraha", "kind": "locality", "name": "Indiranagar", "centroid": [77.6408, 12.9784], "tier_confidence": 0.8},
        {"tenant": "janaagraha", "kind": "pincode", "name": "560038", "centroid": [77.64, 12.97], "tier_confidence": 0.5},
        {"tenant": "othercity", "kind": "locality", "name": "Shivajinagar", "centroid": [73.85, 18.53]},
    ],
    "civic.issue": [
        {"id": "i1", "tenant": "janaagraha", "title": "Potholes in Indiranagar"},
        {"id": "i2", "tenant": "janaagraha", "title": "Flooding at Indiranagar Area underpass"},
        {"id": "i3", "tenant": "janaagraha", "title": "Garbage piles in Shivajinagar"},
        {"id": "i4", "tenant": "janaagraha", "title": "BBMP budget announced"},
        {"id": "i5", "tenant": "janaagraha", "title": "Water cut in Nowhereville"},
        {"id": "i6", "tenant": "janaagraha", "title": "Traffic at Indranagar"},
    ],
}
PLACES = {"i1": "Indiranagar", "i2": "Indiranagar Area", "i3": "Shivajinagar", "i4": None, "i5": "Nowhereville", "i6": "Indranagar"}
writes, geocoder_calls = {}, []
ng.query = lambda scope: list(DATA.get(scope, []))
ng.write = lambda scope, oid, obj: writes.__setitem__(oid, obj)
ng.extract_places = lambda batch: {i: PLACES[it["id"]] for i, it in enumerate(batch) if PLACES[it["id"]]}


def fake_geocode(place):
    geocoder_calls.append((place, ng.CITY_HINT, ng.VIEWBOX))
    return (77.6050, 12.9857, 0.6) if place == "Shivajinagar" else (None, None, 0.0)


ng.geocode = fake_geocode
ng.main()

check("a gazetteer locality is placed from space.place", (writes.get("i1") or {}).get("geometry", {}).get("coordinates") == [77.6408, 12.9784]
      and writes["i1"].get("geocode_source") == "gazetteer:locality", writes.get("i1"))
check("…'Indiranagar Area' matches once the generic word is dropped", (writes.get("i2") or {}).get("geocode_source") == "gazetteer:locality")
check("…and the public geocoder is not asked for gazetteer hits", not any(p in ("Indiranagar", "Indiranagar Area") for p, _, _ in geocoder_calls), geocoder_calls)
check("a locality the gazetteer lacks falls back to the geocoder (another tenant's 'Shivajinagar' row ignored)",
      (writes.get("i3") or {}).get("geocode_source") == "nominatim" and writes["i3"]["geometry"]["coordinates"] == [77.6050, 12.9857])
check("…bounded by the AOI viewbox and the AOI city hint (no hand-set config)",
      geocoder_calls and geocoder_calls[0][1] == "Bengaluru, India" and geocoder_calls[0][2] == "77.46,13.14,77.78,12.83", geocoder_calls[:1])
check("no locality → no point", "i4" not in writes)
check("unresolved locality → no point (never the city centroid)", "i5" not in writes)
check("a near-miss name is not fuzzily matched to the gazetteer ('Indranagar')",
      "i6" not in writes and ("Indranagar", "Bengaluru, India", "77.46,13.14,77.78,12.83") in geocoder_calls)

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
