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
