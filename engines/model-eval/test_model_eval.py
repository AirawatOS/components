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
        assert me.run_settings({"note": "x · cases with expected norms only · answer budget 8000 tokens"}) == ("with_norms", 8000)
        assert me.run_settings({"note": "plain"}) == ("all", 0)
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
