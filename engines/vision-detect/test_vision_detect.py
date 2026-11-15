"""vision-detect — answers in each test's form, rider exclusion, thresholds (no model needed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as vd


def D(cls, score, box, **kw):
    return dict({"class": cls, "score": score, "box": box}, **kw)


def test_uvh_and_coco_classes_map_onto_the_suite_classes():
    assert {vd.TO_CLASS[i] for i in (1, 2, 3, 4, 13)} == {"car"} and 14 not in vd.TO_CLASS
    assert vd.TO_CLASS[7] == "three_wheeler" and vd.TO_CLASS[8] == "two_wheeler"
    assert vd.COCO[1] == "pedestrian" and vd.COCO[21] == "cattle" and vd.COCO[18] == "dog" and vd.COCO[19] == "other_animal"
    assert 3 not in vd.COCO and 16 not in vd.COCO                    # COCO cars (UVH counts vehicles) and birds ignored


def test_answers_respect_the_threshold_and_what_the_pipeline_sees():
    vd.THRESHOLD = 0.5
    dets = [D("car", .9, [0, 0, 1, 1]), D("car", .4, [0, 0, 1, 1]), D("two_wheeler", .8, [0, 0, 1, 1]),
            D("pedestrian", .7, [5, 5, 6, 8]), D("cattle", .6, [9, 9, 12, 12])]
    assert vd.answer("B03", dets)["counts"]["car"] == 1
    assert vd.answer("B04", dets) == {"counts": {"pedestrian": 1}}
    assert vd.answer("B09", dets) == {"counts": {"cattle": 1, "dog": 0, "other_animal": 0}}
    assert vd.answer("B01", dets, ("vehicles",)) == {"labels": ["car", "two_wheeler"]}
    assert vd.answer("B01", dets, ("vehicles", "people_animals")) == {"labels": ["car", "two_wheeler", "pedestrian", "cattle"]}
    assert vd.answer("B01", []) == {"labels": ["none"]}


def test_riders_and_passengers_are_not_pedestrians():
    bike = D("two_wheeler", .9, [100, 200, 160, 300])
    rider = D("pedestrian", .9, [105, 120, 155, 260])                # sits over the bike, reaching above it
    walker = D("pedestrian", .9, [300, 150, 340, 300])               # beside it
    auto = D("three_wheeler", .9, [400, 150, 500, 300]); passenger = D("pedestrian", .8, [420, 170, 470, 260])
    out = vd.mark_riders([bike, rider, walker, auto, passenger])
    assert rider.get("rider") and passenger.get("rider") and not walker.get("rider")
    vd.THRESHOLD = 0.5
    assert vd.answer("B04", out) == {"counts": {"pedestrian": 1}}


def test_vehicle_and_animal_classes_are_the_suite_catalogue_classes():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model-eval"))
    import visual_tests as vt
    assert vd.VEHICLES == vt.VEHICLES and vd.ANIMALS == vt.ANIMALS
