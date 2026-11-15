"""The urban-visual-intelligence test catalogue (ADR-0134), v0.1 — the tests a single camera snapshot can answer with
form labels. Each test: the prompt the model gets, the scorer, and the labelling form's field (what a reviewer fills
in, and where it lands in the case's `expected`). The catalogue is data: adding a test adds an entry here.

Field kinds → expected:
  choice  one option        → expected.labels = "<option>"            (scorer labels)
  multi   options, or none  → expected.labels = ["<option>", …]       (scorer labels)
  counts  integer per class → expected.counts = {"<class>": n, …}     (scorer counts)
  text    named strings     → expected.text   = {"<field>": "…", …}   (scorer text)
"""

SCENE = ("This is a still frame from a fixed traffic camera at a road junction in an Indian city. "
         "Judge only what is visible in this frame.\n\n")
# Vehicle classes match what in-house detectors can produce (UVH-26: AIM@IISc, Bengaluru Safe City cameras), so every
# candidate is scored on the same classes: car = hatchback, sedan, SUV, MUV, van; three_wheeler = autos and
# e-rickshaws; bus = buses, mini-buses, tempo travellers; truck = trucks and light commercial vehicles.
VEHICLES = ["car", "two_wheeler", "three_wheeler", "bus", "truck", "bicycle"]
# Animals on Indian roads. cattle = cows, bulls, buffaloes (COCO detectors call buffaloes cows); other_animal = goats,
# pigs, horses, donkeys, sheep, cats, camels, elephants. Birds are not counted.
ANIMALS = ["cattle", "dog", "other_animal"]

# Shared definitions — the SAME words go to the models (prompts) and to reviewers (form help), so both judge alike.
OCCLUSION = ("Occlusion is about the CAMERA'S VIEW: something between the camera and the scene hides part of what the "
             "camera is meant to watch (the carriageway, the junction, the crossings). vegetation = branches or leaves in "
             "front of the lens or across the road view; cable = wires or cables that hide part of the road (thin wires "
             "across the sky do not count); structure = poles, signboards, hoardings, buildings, flyover parts or the "
             "camera housing hiding road area; vehicle = a stationary vehicle so close to the camera that it hides a large "
             "part of the view (passing traffic does not count). Mark one when it hides roughly a tenth or more of the road "
             "area, or a whole lane, crossing or approach. Glare, haze and darkness are lighting, not occlusion.")
OBSTRUCTION = ("Obstruction is about the ROAD ITSELF: something stationary blocks or narrows the space people move through "
               "— the carriageway or a footpath — so vehicles must swerve or merge, or pedestrians must step into the road. "
               "It counts: vehicles parked in a traffic lane or on a footpath, broken-down vehicles, vendors, carts, stalls "
               "or shop goods spilling out, construction material, debris or garbage heaps, unofficial barricades, cattle "
               "sitting or standing in the road. It does not count: moving traffic or vehicles queued at a signal, vehicles "
               "parked where they leave the lanes and footpath clear, deliberate traffic control (police barricades, cones). "
               "yes = at least one clear obstruction; no = the roads and footpaths you can see are clear; uncertain = the "
               "relevant area cannot be seen (dark, blurred, occluded) or you cannot tell whether something is stationary.")


def _json(spec):
    return "\n\nAnswer with ONLY a JSON object: " + spec


TESTS = [
    {"test_id": "A01", "title": "Image quality", "scorer": "labels",
     "prompt": SCENE + "How usable is this frame for automated traffic analysis? good = clear and well exposed, the "
               "scene can be read; marginal = usable but degraded (blur, low light, compression, partial view); "
               "poor = blank, corrupted, extremely blurred or dark, or the scene cannot be made out."
               + _json('{"label": "good" | "marginal" | "poor"}'),
     "field": {"key": "labels", "kind": "choice", "options": ["good", "marginal", "poor"], "label": "Image quality"}},
    {"test_id": "A02", "title": "Lighting and visibility", "scorer": "labels",
     "prompt": SCENE + "Which lighting and visibility conditions apply? Choose every one that applies: day, night, "
               "glare, haze, low_light, rain." + _json('{"labels": ["day" | "night" | "glare" | "haze" | "low_light" | "rain", …]}'),
     "field": {"key": "labels", "kind": "multi", "options": ["day", "night", "glare", "haze", "low_light", "rain"], "label": "Lighting"}},
    {"test_id": "A04", "title": "Occlusion", "scorer": "labels",
     "prompt": SCENE + OCCLUSION + " Which kinds of occlusion apply to this frame? Choose every one that applies, or "
               "none." + _json('{"labels": ["vegetation" | "cable" | "structure" | "vehicle" | "none", …]}'),
     "field": {"key": "labels", "kind": "multi", "options": ["vegetation", "cable", "structure", "vehicle", "none"], "label": "Occlusion", "help": OCCLUSION}},
    {"test_id": "A06", "title": "Analytics suitability", "scorer": "labels",
     "prompt": SCENE + "Which tasks could this camera view support reliably? vehicle_counting (the carriageway is "
               "visible and vehicles can be told apart), vehicle_tracking (vehicles stay in view long enough to follow), "
               "static_infrastructure (signals, signs, markings, poles can be identified); or none."
               + _json('{"labels": ["vehicle_counting" | "vehicle_tracking" | "static_infrastructure" | "none", …]}'),
     "field": {"key": "labels", "kind": "multi", "options": ["vehicle_counting", "vehicle_tracking", "static_infrastructure", "none"], "label": "Suitable for"}},
    {"test_id": "B01", "title": "Object presence", "scorer": "labels",
     "prompt": SCENE + "Which of these are visible anywhere in the frame? " + ", ".join(VEHICLES + ["pedestrian"] + ANIMALS)
               + "; or none. cattle = cows, bulls, buffaloes; other_animal = goats, pigs, horses, donkeys and other animals "
               "(not birds). Ignore anything inside on-screen text or advertisements."
               + _json('{"labels": [' + " | ".join('"%s"' % v for v in VEHICLES + ["pedestrian"] + ANIMALS + ["none"]) + ", …]}"),
     "field": {"key": "labels", "kind": "multi", "options": VEHICLES + ["pedestrian"] + ANIMALS + ["none"], "label": "Present"}},
    {"test_id": "B03", "title": "Vehicles by class", "scorer": "counts",
     "prompt": SCENE + "Count the vehicles of each class visible in the frame, including partly visible ones. "
               "car = hatchbacks, sedans, SUVs, MUVs and vans; two_wheeler = motorcycles and scooters; three_wheeler = "
               "autos and e-rickshaws; bus = buses, mini-buses and tempo travellers; truck = trucks and light commercial "
               "vehicles (pick-ups, small goods carriers); bicycle = bicycles. Give 0 for a class you do not see."
               + _json('{"counts": {' + ", ".join('"%s": integer' % v for v in VEHICLES) + "}}"),
     "field": {"key": "counts", "kind": "counts", "classes": VEHICLES, "label": "Vehicle counts"}},
    {"test_id": "B04", "title": "Pedestrians", "scorer": "counts",
     "prompt": SCENE + "Count the people on foot visible in the frame, including partly visible ones. Do not count "
               "riders of two-wheelers or bicycles, or people inside vehicles." + _json('{"counts": {"pedestrian": integer}}'),
     "field": {"key": "counts", "kind": "counts", "classes": ["pedestrian"], "label": "Pedestrians"}},
    {"test_id": "B09", "title": "Animals on the road", "scorer": "counts",
     "prompt": SCENE + "Count the animals visible in the frame, including partly visible ones: cattle (cows, bulls, "
               "buffaloes), dog, other_animal (goats, pigs, horses, donkeys, sheep, cats and other animals; not birds). "
               "Give 0 for a class you do not see." + _json('{"counts": {"cattle": integer, "dog": integer, "other_animal": integer}}'),
     "field": {"key": "counts", "kind": "counts", "classes": ANIMALS, "label": "Animals"}},
    {"test_id": "C05", "title": "On-screen text", "scorer": "text",
     "prompt": SCENE + "Read the camera's on-screen overlay text exactly as shown: the date and time stamp, and the "
               "camera or location name. Use an empty string for anything not shown."
               + _json('{"timestamp": "…", "camera_name": "…"}'),
     "field": {"key": "text", "kind": "text", "fields": ["timestamp", "camera_name"], "label": "Overlay text"}},
    {"test_id": "E03", "title": "Obstruction", "scorer": "labels",
     "prompt": SCENE + OBSTRUCTION + " Is there an obstruction in this frame?"
               + _json('{"label": "yes" | "no" | "uncertain", "reason": "one sentence"}'),
     "field": {"key": "labels", "kind": "choice", "options": ["yes", "no", "uncertain"], "label": "Obstruction", "help": OBSTRUCTION}},
]


def expected_from(field, value):
    """A label-form value → the case's expected answer, by field kind (see the module docstring)."""
    if field["kind"] in ("choice", "multi"):
        return {"labels": value}
    if field["kind"] == "counts":
        return {"counts": {k: int(value.get(k) or 0) for k in field["classes"]}}
    return {"text": {k: str(value.get(k) or "") for k in field["fields"]}}
