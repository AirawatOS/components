"""vision-detect — answers in each test's form, rider exclusion, thresholds (no model needed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as vd


def D(cls, score, box, **kw):
    return dict({"class": cls, "score": score, "box": box}, **kw)


def test_uvh_and_coco_classes_map_onto_the_suite_classes():
    assert {vd.TO_CLASS[i] for i in (1, 2, 3, 4, 13)} == {"car"} and 14 not in vd.TO_CLASS
    assert vd.TO_CLASS[7] == "three_wheeler" and vd.TO_CLASS[8] == "two_wheeler"
    # COCO class INDEX (deploy mode): 0 person, 1 bicycle, 3 motorcycle, 14 bird, 16 dog, 19 cow
    assert vd.COCO[0] == "pedestrian" and vd.COCO[19] == "cattle" and vd.COCO[16] == "dog" and vd.COCO[17] == "other_animal"
    assert 1 not in vd.COCO and 3 not in vd.COCO and 14 not in vd.COCO       # bicycles, motorcycles (UVH counts them), birds


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


def test_tiles_cover_the_frame_with_overlap():
    tiles = vd.Detector._tiles(None, 2048, 1536)
    assert tiles[0] == (0, 0, 2048, 1536) and len(tiles) == 5
    quarters = tiles[1:]
    assert min(t[0] for t in quarters) == 0 and max(t[2] for t in quarters) == 2048
    assert min(t[1] for t in quarters) == 0 and max(t[3] for t in quarters) == 1536
    left, right = quarters[0], quarters[2]
    assert right[0] < left[2]                                          # neighbouring quarters overlap


def test_low_confidence_vehicles_do_not_swallow_pedestrians():
    vd.THRESHOLD = 0.5
    phantom = D("truck", .3, [0, 0, 500, 500]); walker = D("pedestrian", .6, [100, 100, 140, 220])
    vd.mark_riders([phantom, walker])
    assert not walker.get("rider")


def test_people_can_have_their_own_threshold():
    vd.THRESHOLD, vd.THRESHOLD_PEOPLE = 0.5, 0.3
    try:
        dets = [D("pedestrian", .35, [0, 0, 1, 2], model="coco"), D("car", .35, [5, 5, 9, 9], model="uvh26")]
        assert vd.answer("B04", dets) == {"counts": {"pedestrian": 1}} and vd.answer("B03", dets)["counts"]["car"] == 0
    finally:
        vd.THRESHOLD_PEOPLE = None
