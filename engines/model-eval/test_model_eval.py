"""model-eval scorers: what earns a score and what does not (no model involved)."""
import os, sys
os.environ.setdefault("RUN_TOKEN", "t")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as me

SECTION = {"messages": [{"role": "user", "content": "Water supply shall be 135 lpcd; a dispensary serves 15,000 persons; refuse 0.3 to 0.6 kg."}], "scorer": "norms"}


def test_numbers_in_reads_commas_and_decimals():
    assert {135.0, 15000.0, 0.3, 0.6} <= me.numbers_in(SECTION["messages"][0]["content"])


def test_empty_answer_scores_zero():
    sc, m = me.score_case(SECTION, "")
    assert sc == 0 and m["answered"] is False


def test_unlabelled_grounded_norms_score_one_and_invented_number_lowers_it():
    good = '[{"metric":"water_supply","operator":"==","value":135,"unit":"lpcd"}]'
    sc, m = me.score_case(SECTION, good)
    assert sc == 1.0 and m["grounded"] == 1
    bad = '[{"metric":"water_supply","operator":"==","value":150,"unit":"lpcd"},{"metric":"disp","operator":"==","value":15000,"unit":"persons"}]'
    sc2, m2 = me.score_case(SECTION, bad)
    assert sc2 == 0.5 and m2["valid"] == 2 and m2["grounded"] == 1


def test_labelled_scores_f1_against_gold():
    case = dict(SECTION, labelled=True, expected={"norms": [{"metric": "w", "value": 135}, {"metric": "d", "value": 15000}]})
    out = '[{"metric":"water","operator":"==","value":135,"unit":"lpcd"}]'
    sc, m = me.score_case(case, out)
    assert m["recall"] == 0.5 and m["precision"] == 1.0 and abs(sc - 0.667) < 0.01


def test_labelled_section_with_no_norms_rewards_an_empty_list():
    case = dict(SECTION, labelled=True, expected={"norms": []})
    assert me.score_case(case, "[]")[0] == 1.0
    assert me.score_case(case, '[{"metric":"w","operator":"==","value":135}]')[0] == 0.0


def test_range_values_match_in_any_order():
    case = dict(SECTION, labelled=True, expected={"norms": [{"metric": "refuse", "value": [0.3, 0.6]}]})
    assert me.score_case(case, '[{"metric":"r","operator":"between","value":[0.6,0.3]}]')[0] == 1.0


def test_contains_exact_json_fields_grounded():
    assert me.score_case({"scorer": "contains", "expected": {"contains": ["Jayanagar", "135"]}}, "near Jayanagar, 135 lpcd")[0] == 1.0
    assert me.score_case({"scorer": "exact", "expected": {"answer": "roads"}}, " roads ")[0] == 1.0
    assert me.score_case({"scorer": "json_fields", "expected": {"fields": {"category": "water", "severity": "high"}}},
                         'x {"category":"Water","severity":"low"} y')[0] == 0.5
    g = {"scorer": "grounded", "messages": [{"role": "user", "content": "density 67,183 per km2"}]}
    assert me.score_case(g, "about 67,183 people per km2")[0] == 1.0
    assert me.score_case(g, "about 70,000 people per km2")[0] == 0.0


def test_lakh_and_crore_are_their_plain_numbers():
    ns = me.numbers_in("District jail per 10 lakh; radio station per 5-8 lakh; head post office 2.5 lakh; budget 3 crore; 4 lakhs")
    for v in (1_000_000, 500_000, 800_000, 250_000, 30_000_000, 400_000):
        assert float(v) in ns, v
    assert 10.0 in ns                                         # the written number itself still counts


def test_a_norm_written_in_plain_numbers_from_a_lakh_figure_is_grounded():
    case = {"scorer": "norms", "messages": [{"role": "user", "content": "Police line: 20 lakh population; radio/TV 5 to 8 lakh."}]}
    out = '[{"metric":"pop_per_police_line","operator":"==","value":2000000},{"metric":"pop_per_radio","operator":"between","value":[500000,800000]}]'
    sc, m = me.score_case(case, out)
    assert sc == 1.0 and m["grounded"] == 2 and m["not_grounded"] == []


def test_metrics_keep_the_parsed_norms():
    sc, m = me.score_case(SECTION, '[{"metric":"water_supply","operator":"==","value":135,"unit":"lpcd"}]')
    assert m["norms"] == [{"metric": "water_supply", "operator": "==", "value": 135, "unit": "lpcd"}]


def test_with_norms_keeps_only_labelled_cases_that_expect_a_norm():
    assert me.has_norms({"labelled": True, "expected": {"norms": [{"metric": "x", "operator": "==", "value": 1}]}})
    assert not me.has_norms({"labelled": True, "expected": {"norms": []}})
    assert not me.has_norms({"labelled": False, "expected": {"norms": [{"metric": "x", "operator": "==", "value": 1}]}})
    assert not me.has_norms({"labelled": True})


def test_run_budget_overrides_the_case_budget():
    me.MAX_TOKENS = 0
    assert me.budget({"max_tokens": 2500}) == 2500 and me.budget({}) == 1024
    me.MAX_TOKENS = 8000
    try:
        assert me.budget({"max_tokens": 2500}) == 8000
    finally:
        me.MAX_TOKENS = 0


def test_a_resumed_run_keeps_the_settings_its_note_recorded():
    me.MAX_TOKENS, me.CASES = 0, "all"
    old = os.environ.pop("CASES", None)
    try:
        assert me.run_settings({"note": "x · cases with expected norms only · answer budget 8000 tokens · thinking on"}) == ("with_norms", 8000, "on")
        assert me.run_settings({"note": "plain"}) == ("all", 0, "")
    finally:
        if old is not None:
            os.environ["CASES"] = old


def _fake_run(monkey_results, stop_after):
    """Run run_one against in-memory records: 2 models x 2 cases, with `monkey_results` already written."""
    cases = [{"id": "ds:t:intelligence/eval_case/c%d" % i, "suite": "s", "status": "active", "scorer": "exact",
              "messages": [{"role": "user", "content": "q"}], "expected": {"answer": "a"}, "labelled": True} for i in (1, 2)]
    store = {"intelligence.eval_case": cases, "intelligence.eval_result": list(monkey_results)}
    writes, asked = [], []
    saved = (me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: writes.append((scope, obj))
    me.ask = lambda model, c: (asked.append((model, c["id"])) or ("a", model, {}, None, 5))
    me.T, me.STOP_AFTER, me.MAX_TOKENS = "t", stop_after, 0
    try:
        me.run_one({"id": "ds:t:intelligence/eval_run/r1", "suite": "s", "models": ["m1", "m2"], "status": "pending"})
    finally:
        me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS = saved
    runs = [o for s_, o in writes if s_ == "intelligence.eval_run"]
    return asked, runs


def test_a_run_past_its_time_pauses_and_stays_pending():
    asked, runs = _fake_run([], stop_after=-1)
    assert asked == [] and runs[-1]["status"] == "pending" and "paused with 4 answers to go" in runs[-1]["note"]


def test_a_resumed_run_asks_only_what_is_missing_and_finishes():
    done = [{"run_ref": "ds:t:intelligence/eval_run/r1", "model": "m1", "case_ref": "ds:t:intelligence/eval_case/c%d" % i,
             "score": 1.0, "metrics": {"answered": True}, "latency_ms": 5} for i in (1, 2)]
    asked, runs = _fake_run(done, stop_after=10_000)
    assert asked == [("m2", "ds:t:intelligence/eval_case/c1"), ("m2", "ds:t:intelligence/eval_case/c2")]
    assert runs[-1]["status"] == "done" and runs[-1]["summary"]["m1"]["cases"] == 2 and runs[-1]["summary"]["m2"]["cases"] == 2


def test_json_fields_compare_numbers_within_one_percent():
    case = {"scorer": "json_fields", "expected": {"fields": {"land_ha": 4.5, "stations": 3, "ok": False, "src": "state"}}}
    sc, m = me.score_case(case, '```json\n{"land_ha": "4.50", "stations": 3.0, "ok": false, "src": "State"}\n```')
    assert sc == 1.0 and m["wrong"] == []
    sc, m = me.score_case(case, '{"land_ha": 4.6, "stations": 3, "ok": true, "src": "state"}')
    assert sc == 0.5 and sorted(m["wrong"]) == ["land_ha", "ok"]


def test_json_fields_reads_the_last_object_after_working():
    case = {"scorer": "json_fields", "expected": {"fields": {"stations": 3}}}
    out = 'Step 1: {235000 / 90000} = 2.6, so round up.\nDraft: {"stations": 2}\nCorrected.\n{"stations": 3}'
    sc, m = me.score_case(case, out)
    assert sc == 1.0 and m["parsed"]


def test_thinking_switch_goes_to_the_gateway_only_when_set():
    saved = me.THINKING
    try:
        me.THINKING = "on"; assert me.thinking_arg() == {"thinking": True}
        me.THINKING = "off"; assert me.thinking_arg() == {"thinking": False}
        me.THINKING = ""; assert me.thinking_arg() == {}
    finally:
        me.THINKING = saved


# ---- ADR-0134: images, privacy, counts / labels / text -------------------------------------------------------------
import base64 as _b64
import io

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20
IMG_CASE = {"id": "ds:t:intelligence/eval_case/x", "visibility": "private", "inputs": [{"kind": "image", "file_id": "sha2-256:ab"}],
            "messages": [{"role": "user", "content": "Count the vehicles."}], "scorer": "counts"}


def _gateway(serves):
    """A fake broker /v1/complete: `serves` maps model → hosting the gateway would report ('in_house' | 'external' |
    None for an older gateway that ignores require_in_house). Returns (call, sent bodies)."""
    sent = []

    def call(path, body, timeout=180):
        if path == "/v1/file/get":
            return {"content_b64": _b64.b64encode(JPEG).decode()}
        sent.append(body)
        h = serves.get(body.get("model"), "external")
        if body.get("require_in_house") and h == "external":
            raise me.urllib.error.HTTPError(path, 403, "refused", {}, io.BytesIO(b'{"error": "refused"}'))
        return dict({"content": "{}", "model": body.get("model")}, **({"hosting": h} if h else {}))
    return call, sent


def _private_case(fn):
    saved = (me.IN_HOUSE, me.TASKS, me.call)
    me.IN_HOUSE, me.TASKS = {}, {}
    try:
        return fn()
    finally:
        me.IN_HOUSE, me.TASKS, me.call = saved


def _has_image(body):
    return any(isinstance(m.get("content"), list) for m in body.get("messages") or [])


def test_private_image_case_is_never_sent_to_an_external_model():
    def go():
        me.call, sent = _gateway({"local-gemma": "in_house"})
        out, served, usage, err, ms = me.ask("google/gemma-4-31B-it", IMG_CASE)
        assert err and err.startswith("refused: private case (camera media)")
        assert sent and not any(_has_image(b) for b in sent)            # only the data-free probe went out
        assert all(b["messages"] == me.PROBE and b["require_in_house"] for b in sent)
    _private_case(go)


def test_an_older_gateway_that_ignores_the_flag_gets_nothing_private():
    def go():
        me.call, sent = _gateway({"kimi-k3": None})                     # answers, but without `hosting`
        err = me.ask("kimi-k3", IMG_CASE)[3]
        assert "does not enforce" in err and len(sent) == 1 and not _has_image(sent[0])
    _private_case(go)


def test_an_in_house_model_gets_the_frame_with_the_flag_set():
    def go():
        me.call, sent = _gateway({"kimi-k3": "in_house"})
        assert me.ask("kimi-k3", IMG_CASE)[3] is None
        assert me.ask("kimi-k3", IMG_CASE)[3] is None                   # probed once per model, not per case
        assert [_has_image(b) for b in sent] == [False, True, True]
        assert all(b["require_in_house"] for b in sent)
    _private_case(go)


def test_a_public_text_case_is_not_probed():
    def go():
        me.call, sent = _gateway({})
        assert me.ask("claude-sonnet-5-5", {"id": "x", "messages": [{"role": "user", "content": "Hi"}]})[3] is None
        assert len(sent) == 1 and "require_in_house" not in sent[0]
    _private_case(go)


def test_images_go_as_data_url_parts_after_the_text():
    saved = me.call
    me.call = lambda path, body, timeout=180: {"content_b64": _b64.b64encode(JPEG).decode()} if path == "/v1/file/get" else {}
    try:
        msgs = me.with_images(IMG_CASE)
        parts = msgs[0]["content"]
        assert parts[0] == {"type": "text", "text": "Count the vehicles."}
        assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert IMG_CASE["messages"][0]["content"] == "Count the vehicles."                 # the case itself is untouched
    finally:
        me.call = saved


def test_a_non_image_input_is_an_error_not_a_silent_send():
    saved = me.call
    me.call = lambda path, body, timeout=180: {"content_b64": _b64.b64encode(b"%PDF-1.7").decode()}
    try:
        try:
            me.with_images(IMG_CASE); assert False, "should refuse"
        except ValueError as e:
            assert "not a JPEG" in str(e)
    finally:
        me.call = saved


def test_counts_score_mae_exact_and_bias():
    case = {"scorer": "counts", "expected": {"counts": {"car": 4, "two_wheeler": 6, "bus": 0}}, "labelled": True}
    sc, m = me.score_case(case, '{"counts": {"car": 5, "two_wheeler": 6}}')         # missing class counts as 0
    assert sc == 0.9 and m["mae"] == 0.333 and m["exact"] == 0.667 and m["bias"] == 0.333
    assert me.score_case(case, '{"car": 4, "two_wheeler": 6, "bus": 0}')[0] == 1.0   # flat object accepted
    assert me.score_case(case, "no idea")[0] == 0.0


def test_labels_f1_single_multi_and_uncertain():
    multi = {"scorer": "labels", "expected": {"labels": ["day", "glare"]}, "labelled": True}
    assert me.score_case(multi, '{"labels": ["Day", "glare"]}')[0] == 1.0
    assert me.score_case(multi, '{"labels": ["day"]}')[0] == 0.667
    one = {"scorer": "labels", "expected": {"labels": "uncertain"}, "labelled": True}
    assert me.score_case(one, '{"label": "uncertain"}')[0] == 1.0
    assert me.score_case(one, '{"label": "yes"}')[0] == 0.0


def test_text_cer_and_exact():
    case = {"scorer": "text", "expected": {"text": {"timestamp": "10-16-2026 Fri 11:46:22", "camera": "ALLENGANJ BOX1"}}, "labelled": True}
    sc, m = me.score_case(case, '{"timestamp": "10-16-2026 Fri 11:46:22", "camera": "allenganj  box1"}')
    assert sc == 1.0 and m["exact"] == 1.0
    sc, m = me.score_case(case, '{"timestamp": "10-16-2026 Fri 11:46:2", "camera": "ALLENGANJ BOX1"}')
    assert 0.97 < sc < 1.0 and m["exact"] == 0.5


def test_site_view_and_split_from_camera_file_names():
    assert me.site_and_view("BANNS_MANDI_CHAURAHA_BOX2.jpg") == ("banns-mandi-chauraha", "box")
    assert me.site_and_view("BARRA_BYPAS_CHAURAHA__PTZ.jpg") == ("barra-bypas-chauraha", "ptz")
    assert me.site_and_view("ALLENGANJ_Box4.jpg") == ("allenganj", "box")
    assert me.site_and_view("some photo.png") == ("some-photo", "other")
    assert me.split_of("allenganj", 0.33) == me.split_of("allenganj", 0.33)          # stable


def test_seed_images_makes_private_tagged_cases_and_keeps_existing_ones():
    store = {"platform.file": [
        {"id": "ds:t:file/1", "kind": "file", "folder": "/frames", "name": "ALLENGANJ_BOX1.jpg", "file_id": "sha2-256:a", "content_type": "image/jpeg"},
        {"id": "ds:t:file/2", "kind": "file", "folder": "/frames", "name": "ALLENGANJ_PTZ.jpg", "file_id": "sha2-256:b", "content_type": "image/jpeg"},
        {"id": "ds:t:file/3", "kind": "file", "folder": "/frames", "name": "notes.txt", "file_id": "sha2-256:c", "content_type": "text/plain"},
        {"id": "ds:t:file/4", "kind": "file", "folder": "/other", "name": "X_BOX1.jpg", "file_id": "sha2-256:d", "content_type": "image/jpeg"}],
        "intelligence.eval_case": [{"id": "ds:t:intelligence/eval_case/urban-visual-intelligence--b03--allenganj-box1-jpg", "suite": "urban-visual-intelligence"}]}
    wrote = []
    saved = (me.mine, me.write, me.T)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: wrote.append(obj)
    me.T = "t"
    os.environ.update({"SEED_FOLDER": "/frames", "SEED_TESTS": "B03,E03"})
    try:
        me.seed_images()
    finally:
        me.mine, me.write, me.T = saved
        for k in ("SEED_FOLDER", "SEED_TESTS"):
            os.environ.pop(k, None)
    ids = sorted(o["id"].split("/")[-1] for o in wrote)
    assert ids == ["urban-visual-intelligence--b03--allenganj-ptz-jpg", "urban-visual-intelligence--e03--allenganj-box1-jpg",
                   "urban-visual-intelligence--e03--allenganj-ptz-jpg"]                     # existing B03 case kept, text + other folder skipped
    c = next(o for o in wrote if o["id"].endswith("e03--allenganj-ptz-jpg"))
    assert c["visibility"] == "private" and c["scorer"] == "labels" and c["labelled"] is False and c["inputs"][0]["file_id"] == "sha2-256:b"
    assert "site:allenganj" in c["tags"] and "view:ptz" in c["tags"] and "test:E03" in c["tags"]
    assert any(t.startswith("split:") for t in c["tags"]) and c["label_form"][0]["options"] == ["yes", "no", "uncertain"]


def test_visual_catalogue_is_complete_and_each_field_maps_to_its_scorer():
    import visual_tests as vt
    assert [t["test_id"] for t in vt.TESTS] == ["A01", "A02", "A04", "A06", "B01", "B03", "B04", "B09", "C05", "E03"]
    assert "cattle" in vt.TESTS[4]["field"]["options"] and vt.TESTS[2]["field"]["help"] == vt.OCCLUSION and vt.TESTS[-1]["field"]["help"] == vt.OBSTRUCTION
    kinds = {"labels": ("choice", "multi"), "counts": ("counts",), "text": ("text",)}
    for t in vt.TESTS:
        assert t["field"]["kind"] in kinds[t["scorer"]], t["test_id"]
        assert "Answer with ONLY a JSON object" in t["prompt"]
    assert vt.expected_from(vt.TESTS[5]["field"], {"car": "3"}) == {"counts": {c: (3 if c == "car" else 0) for c in vt.VEHICLES}}


def test_component_subjects_are_left_to_their_engine():
    assert me.is_component("component:uvh26-rtdetrv2-s") and not me.is_component("claude-sonnet-5-5")
    case = {"id": "ds:t:intelligence/eval_case/c1", "suite": "s", "status": "active", "scorer": "exact", "messages": [{"role": "user", "content": "q"}]}
    asked = []
    saved = (me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS)
    me.mine = lambda scope, limit=20000: [case] if scope == "intelligence.eval_case" else []
    me.write = lambda scope, obj: None
    me.ask = lambda model, c: (asked.append(model) or ("a", model, {}, None, 5))
    me.T, me.STOP_AFTER, me.MAX_TOKENS = "t", 10_000, 0
    try:
        me.run_one({"id": "ds:t:intelligence/eval_run/r", "suite": "s", "models": ["component:uvh26-rtdetrv2-s", "m1"], "status": "pending"})
    finally:
        me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS = saved
    assert asked == ["m1"]


def test_seed_update_refreshes_only_unlabelled_cases():
    import visual_tests as vt
    b03 = next(t for t in vt.TESTS if t["test_id"] == "B03")
    old = {"id": "ds:t:intelligence/eval_case/urban-visual-intelligence--b03--a-box1-jpg", "suite": "urban-visual-intelligence",
           "messages": [{"role": "user", "content": "old prompt"}], "label_form": [], "scorer": "counts", "labelled": False}
    lab = dict(old, id="ds:t:intelligence/eval_case/urban-visual-intelligence--b03--b-box1-jpg", labelled=True)
    store = {"platform.file": [{"id": "1", "kind": "file", "folder": "/f", "name": "A_BOX1.jpg", "file_id": "sha2-256:a", "content_type": "image/jpeg"},
                               {"id": "2", "kind": "file", "folder": "/f", "name": "B_BOX1.jpg", "file_id": "sha2-256:b", "content_type": "image/jpeg"}],
             "intelligence.eval_case": [old, lab]}
    wrote = []
    saved = (me.mine, me.write, me.T)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: wrote.append(obj)
    me.T = "t"
    os.environ.update({"SEED_FOLDER": "/f", "SEED_TESTS": "B03", "SEED_UPDATE": "1"})
    try:
        me.seed_images()
    finally:
        me.mine, me.write, me.T = saved
        for k in ("SEED_FOLDER", "SEED_TESTS", "SEED_UPDATE"):
            os.environ.pop(k, None)
    assert [o["id"] for o in wrote] == [old["id"]]                       # labelled case untouched, nothing new made
    assert wrote[0]["messages"][0]["content"] == b03["prompt"] and wrote[0]["label_form"] == [b03["field"]]
    assert b03["field"]["classes"] == ["car", "two_wheeler", "three_wheeler", "bus", "truck", "bicycle"]


def test_rescore_keeps_a_detectors_boxes():
    case = {"id": "ds:t:intelligence/eval_case/c1", "suite": "s", "scorer": "counts", "expected": {"counts": {"car": 2}}, "labelled": True}
    res = {"id": "x", "run_ref": "ds:t:intelligence/eval_run/r", "case_ref": case["id"], "model": "component:d", "output": '{"counts": {"car": 2}}',
           "metrics": {"detections": [{"class": "car", "box": [0, 0, 1, 1]}], "threshold": 0.5}, "score": None}
    store = {"intelligence.eval_run": [{"id": "ds:t:intelligence/eval_run/r", "suite": "s", "models": ["component:d"], "status": "done"}],
             "intelligence.eval_case": [case], "intelligence.eval_result": [res]}
    wrote = []
    saved = (me.mine, me.write, me.T)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: wrote.append((scope, obj))
    me.T = "t"
    try:
        me.rescore("ds:t:intelligence/eval_run/r", False)
    finally:
        me.mine, me.write, me.T = saved
    r = next(o for s_, o in wrote if s_ == "intelligence.eval_result")
    assert r["score"] == 1.0 and r["metrics"]["detections"] and r["metrics"]["threshold"] == 0.5 and r["metrics"]["mae"] == 0


def test_unlabelled_image_cases_get_no_score_and_summaries_skip_them():
    sc, m = me.score_case({"scorer": "counts", "expected": None, "labelled": False}, '{"counts": {"car": 0}}')
    assert sc is None and m["unlabelled"]
    s = me.summarise({"models": ["m"]}, [{"model": "m", "score": 0.5, "metrics": {"answered": True}}, {"model": "m", "score": None, "metrics": {"answered": True}}])
    assert s["m"]["score"] == 0.5 and s["m"]["scored"] == 1 and s["m"]["cases"] == 2


def test_a_run_over_unlabelled_image_cases_completes():
    # 0.2.5 regression: an unlabelled case's score is None and the progress line formatted it as a number → crash
    case = {"id": "ds:t:intelligence/eval_case/u", "suite": "s", "status": "active", "scorer": "counts", "test_id": "B03",
            "messages": [{"role": "user", "content": "q"}], "expected": None, "labelled": False}
    store = {"intelligence.eval_case": [case], "intelligence.eval_result": []}
    writes = []
    saved = (me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: writes.append((scope, obj))
    me.ask = lambda model, c: ('{"counts": {"car": 1}}', model, {}, None, 5)
    me.T, me.STOP_AFTER, me.MAX_TOKENS = "t", 1400, 0
    try:
        me.run_one({"id": "ds:t:intelligence/eval_run/u", "suite": "s", "models": ["m"], "status": "pending"})
    finally:
        me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS = saved
    res = [o for s_, o in writes if s_ == "intelligence.eval_result"]
    run = [o for s_, o in writes if s_ == "intelligence.eval_run"][-1]
    assert len(res) == 1 and res[0]["score"] is None and run["status"] == "done"


def test_mine_follows_every_page():
    # 0.2.7: the broker pages; reading only the first page dropped cases (B09, C05, E03 never ran on staging)
    pages = {None: {"results": [{"object": {"id": "ds:t:x/1"}}], "next_cursor": "c1"},
             "c1": {"results": [{"object": {"id": "ds:t:x/2"}}, {"object": {"id": "ds:other:x/3"}}], "next_cursor": None}}
    saved = (me.call, me.T)
    me.call, me.T = (lambda path, body, timeout=180: pages[body.get("cursor")]), "t"
    try:
        assert [o["id"] for o in me.mine("x")] == ["ds:t:x/1", "ds:t:x/2"]
    finally:
        me.call, me.T = saved


def test_a_drafting_run_takes_only_the_named_tests_and_unlabelled_cases_and_says_so():
    mk = lambda t, lab, i: {"id": "ds:t:intelligence/eval_case/s--%s--%d" % (t, i), "suite": "s", "status": "active", "test_id": t,
                            "scorer": "labels", "messages": [{"role": "user", "content": "q"}], "labelled": lab,
                            "expected": {"labels": "good"} if lab else None}
    store = {"intelligence.eval_case": [mk("A01", True, 1), mk("A01", False, 2), mk("B03", False, 3), mk("C05", False, 4)],
             "intelligence.eval_result": []}
    writes, asked = [], []
    saved = (me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS, me.ONLY_TESTS, me.ONLY_UNLABELLED)
    me.mine = lambda scope, limit=20000: list(store.get(scope, []))
    me.write = lambda scope, obj: writes.append((scope, obj))
    me.ask = lambda model, c: (asked.append(c["id"]) or ('{"label": "good"}', model, {}, None, 5))
    me.T, me.STOP_AFTER, me.MAX_TOKENS, me.ONLY_TESTS, me.ONLY_UNLABELLED = "t", 1400, 0, ["A01", "C05"], True
    try:
        me.run_one({"id": "ds:t:intelligence/eval_run/d", "suite": "s", "models": ["m"], "status": "pending"})
    finally:
        me.mine, me.write, me.ask, me.T, me.STOP_AFTER, me.MAX_TOKENS, me.ONLY_TESTS, me.ONLY_UNLABELLED = saved
    assert sorted(a.rsplit("--", 2)[-2] for a in asked) == ["A01", "C05"]
    run = [o for s_, o in writes if s_ == "intelligence.eval_run"][-1]
    assert "tests A01,C05 only" in run["note"] and "unlabelled cases only" in run["note"]
