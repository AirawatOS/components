"""vision-detect — the detector's answers in the form each test asks for, from UVH-26 classes (no model needed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as vd


def test_uvh_classes_map_onto_the_suite_classes():
    assert {vd.TO_CLASS[i] for i in (1, 2, 3, 4, 13)} == {"car"}
    assert {vd.TO_CLASS[i] for i in (5, 10, 11)} == {"bus"} and {vd.TO_CLASS[i] for i in (6, 9)} == {"truck"}
    assert vd.TO_CLASS[7] == "three_wheeler" and vd.TO_CLASS[8] == "two_wheeler" and vd.TO_CLASS[12] == "bicycle"
    assert 14 not in vd.TO_CLASS                                          # "Others" is not counted


def test_answers_for_b03_and_b01():
    dets = [{"class": "car"}, {"class": "car"}, {"class": "two_wheeler"}, {"class": "other"}]
    assert vd.answer("B03", dets) == {"counts": {"car": 2, "two_wheeler": 1, "three_wheeler": 0, "bus": 0, "truck": 0, "bicycle": 0}}
    assert vd.answer("B01", dets) == {"labels": ["car", "two_wheeler"]}
    assert vd.answer("B01", []) == {"labels": ["none"]}


def test_vehicle_classes_are_the_suite_catalogue_classes():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model-eval"))
    import visual_tests as vt
    assert vd.VEHICLES == vt.VEHICLES
