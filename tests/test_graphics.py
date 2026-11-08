"""Animated graphics: spec validation, number sourcing, placement on the right shot, rendering."""
from pathlib import Path

from pipeline import captions, footage, graphics, script
from pipeline.config import load_config
from pipeline.media import probe

FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Bold.ttf"
COMPARE = {"type": "compare", "items": [{"label": "Normal rack", "value": 10, "unit": "kW"},
                                        {"label": "AI rack", "value": 60, "unit": "kW"}]}


def test_spec_validation():
    assert graphics.validate_spec({"type": "counter", "value": "2.2", "unit": "GW", "label": "one data center"}) == []
    assert graphics.validate_spec(COMPARE) == []
    assert graphics.validate_spec({"type": "steps", "items": ["a", "b", "c"]}) == []
    assert graphics.validate_spec({"type": "pie"})  # unknown type
    assert graphics.validate_spec({"type": "counter", "value": "two point two"})  # digits only
    assert graphics.validate_spec({"type": "percent", "value": 140, "label": "x"})  # 0-100
    assert graphics.validate_spec({"type": "compare", "items": [COMPARE["items"][0]]})  # needs 2
    apples_oranges = {"type": "compare", "items": [{"label": "AQI", "value": 999, "unit": ""},
                                                   {"label": "Cigarettes", "value": 45, "unit": "cigs"}]}
    assert any("same unit" in i for i in graphics.validate_spec(apples_oranges))
    assert graphics.validate_spec({"type": "steps", "items": ["only one"]})


def test_numbers_are_normalised_for_comparison():
    assert script.numbers_in("about 1,000 NPCs, 2.20 GW and 5.5x.") == {"1000", "2.2", "5.5"}
    assert graphics.spec_text(COMPARE) == "Normal rack: 10 kW vs AI rack: 60 kW"


def test_script_graphic_numbers_must_come_from_facts():
    cfg = load_config()
    beat = {"role": "mechanism", "narration": "x", "on_screen_text": "x", "visual_keywords": ["a"],
            "fact_ids": ["F1"], "graphic": COMPARE}
    s = {"hook": "h", "beats": [beat], "cta": "c", "title": "t"}
    issues = script.validate(s, cfg, {"F1"}, stated={"10", "60"})
    assert not any("graphic" in i for i in issues)
    issues = script.validate(s, cfg, {"F1"}, stated={"10"})
    assert any("numbers no fact states (60)" in i for i in issues)
    s["beats"] = [dict(beat) for _ in range(3)]
    assert any("use at most" in i for i in script.validate(s, cfg, {"F1"}, stated={"10", "60"}))


def test_bad_graphics_are_dropped_not_fatal():
    beats = [{"graphic": {"type": "compare", "items": [{"label": "AQI", "value": 999, "unit": ""},
                                                       {"label": "Cigs", "value": 45, "unit": "cigs"}]}},
             {"graphic": COMPARE}, {"graphic": COMPARE}, {"narration": "no graphic"}]
    draft = {"beats": beats}
    issues = ["beat 1 graphic: compare items must be the same kind", "3 beats have a graphic; use at most 2"]
    assert script.drop_bad_graphics(draft, issues, max_graphics=1)
    assert [("graphic" in b) for b in draft["beats"]] == [False, True, False, False]
    # any non-graphic problem means the script really is wrong: leave it to the retry loop
    assert not script.drop_bad_graphics({"beats": [{"graphic": COMPARE}]}, ["hook is missing"], 2)


def test_stray_graphic_is_dropped_when_graphics_are_off():
    cfg = load_config()
    cfg.raw.setdefault("video", {})["graphics"] = False
    draft = {"beats": [{"narration": "x", "graphic": {"type": "pie"}}]}
    assert "graphic" not in script._drop_graphics_unless_enabled(draft, cfg)["beats"][0]
    cfg.raw["video"]["graphics"] = True
    draft = {"beats": [{"narration": "x", "graphic": COMPARE}]}
    assert script._drop_graphics_unless_enabled(draft, cfg)["beats"][0]["graphic"] == COMPARE


def test_graphic_goes_on_the_shot_where_the_number_is_spoken():
    s = {"hook": "h", "beats": [{"narration": "n", "visual_keywords": ["a b", "c d"], "graphic": COMPARE}]}
    words = [{"text": w, "start": t, "segment": "beat1"} for w, t in
             (("Each", 2.0), ("AI", 3.0), ("rack", 4.5), ("draws", 6.0), ("60", 6.8), ("kilowatts", 7.2))]
    timing = {"duration": 9.0, "segments": [{"name": "hook", "slot_end": 2.0}, {"name": "beat1", "start": 2.0}],
              "words": words}
    shots = footage.build_shots(s, timing, graphics=True)
    with_graphic = [x for x in shots if x.get("graphic")]
    assert len(with_graphic) == 1 and with_graphic[0]["slot"] == "beat1b"  # "60" is in the second half
    assert with_graphic[0]["graphic_at"] == round(6.8 - with_graphic[0]["start"] - 0.1, 2)
    assert not any(x.get("graphic") for x in footage.build_shots(s, timing, graphics=False))


def test_no_headline_over_a_graphic():
    shots = [{"slot": "hook", "start": 0, "end": 2, "kind": "stock", "on_screen_text": "h"},
             {"slot": "beat1", "start": 2, "end": 5, "kind": "graphic", "on_screen_text": "6x"},
             {"slot": "beat2", "start": 5, "end": 8, "kind": "stock", "on_screen_text": "next"}]
    assert [h[2] for h in captions.headlines_from_shots(shots)] == ["next"]


def test_render_makes_an_exact_length_vertical_clip(tmp_path):
    out = graphics.render(COMPARE, tmp_path / "g.mp4", 0.6, 10, FONT, start_at=0.1)
    p = probe(out)
    assert (p.width, p.height) == (1080, 1920) and abs(p.duration - 0.6) < 0.15
