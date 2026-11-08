"""Phase 2 content stages with a fake LLM and mocked HTTP (no keys or network needed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import factcheck, research, script, trends
from pipeline.config import load_config
from pipeline.llm import LLM, FakeProvider
from pipeline.stages import AbortRun, StageContext
from pipeline.state import Ledger, RunState, read_json

WIKI_TEXT = (
    "Rayleigh scattering is the scattering of light by particles much smaller than the wavelength of the light. "
    "The intensity of Rayleigh scattering is inversely proportional to the fourth power of the wavelength, "
    "so blue light is scattered much more strongly than red light. "
    "Blue light at 450 nanometres is scattered about 5.5 times more than red light at 700 nanometres. "
    "Violet light is scattered even more than blue, but the sky does not look violet because sunlight contains "
    "less violet light and human eyes are less sensitive to violet. "
    "At sunset, sunlight travels through more of the atmosphere, so most blue light is scattered away "
    "and the remaining light appears red and orange. "
) * 2

FACTS = [
    {"id": "F1", "fact": "Blue light is scattered far more than red light.",
     "source_id": "S1", "quote": "so blue light is scattered much more strongly than red light"},
    {"id": "F2", "fact": "Blue is scattered about 5.5 times more than red.",
     "source_id": "S1", "quote": "Blue light at 450 nanometres is scattered about 5.5 times more than red light at 700 nanometres."},
    {"id": "F3", "fact": "Violet scatters more, but sunlight has less violet and eyes are less sensitive to it.",
     "source_id": "S1", "quote": "Violet light is scattered even more than blue, but the sky does not look violet"},
    {"id": "F9", "fact": "Made-up fact the model hallucinated.",
     "source_id": "S1", "quote": "The sky is blue because it reflects the colour of the ocean on sunny days."},
]

WIKI_URL = "https://en.wikipedia.org/wiki/Rayleigh_scattering"
VERIFIED = [dict(f, source_title="Wikipedia: Rayleigh scattering", source_url=WIKI_URL) for f in FACTS[:3]]

GOOD_SCRIPT = {
    "hook_options": [{"text": "The sky should actually be violet.", "type": "counterintuitive"}],
    "hook": "The sky should actually be violet.",
    "beats": [
        {"role": "open_loop", "narration": "Sunlight looks white, but it is every colour mixed together, and the reason you see blue up there is stranger than you think. Stick around for the twist.",
         "on_screen_text": "White light = all colours", "visual_keywords": ["sunlight through clouds"], "fact_ids": []},
        {"role": "mechanism", "narration": "When sunlight hits the air, tiny gas molecules bounce some of it around. Blue light gets scattered far more than red light, so it spreads across the whole sky.",
         "on_screen_text": "Blue scatters most", "visual_keywords": ["blue sky timelapse"], "fact_ids": ["F1"]},
        {"role": "mechanism", "narration": "How much more? Blue gets scattered about five and a half times more than red. Think of it like a crowded train where small kids get pushed around the most.",
         "on_screen_text": "5.5x more", "visual_keywords": ["crowded train platform"], "fact_ids": ["F2"]},
        {"role": "rehook", "narration": "But here's the catch. Violet light scatters even more than blue. So by that logic, you should be looking up at a purple sky every single afternoon.",
         "on_screen_text": "So why not violet?", "visual_keywords": ["violet light prism"], "fact_ids": ["F3"]},
        {"role": "payoff", "narration": "The answer is you. Sunlight contains less violet to begin with, and your eyes are far less sensitive to it. The sky is violet-blue, and your brain rounds it to blue.",
         "on_screen_text": "Your eyes decide", "visual_keywords": ["human eye close up"], "fact_ids": ["F3"]},
    ],
    "cta": "Follow, because the sky should actually be",
    "title": "Why the sky isn't actually violet",
    "description_summary": "Why scattering makes the sky blue, and why it isn't violet.",
    "tags": ["science", "physics", "sky", "light", "rayleigh scattering", "explained", "shorts", "facts"],
}


def responder(script_queue: list[dict], check_queue: list[dict], patch_queue: list[dict] | None = None):
    def respond(prompt: str) -> str:
        if "LINES FLAGGED BY THE FACT-CHECKER" in prompt:
            return json.dumps(patch_queue.pop(0) if patch_queue else GOOD_SCRIPT)
        if "Items:" in prompt:
            return json.dumps({"items": [
                {"i": 0, "niche_relevance": 0.2, "hook_potential": 0.9, "angle": "x", "search_queries": [], "avoid": False},
                {"i": 1, "niche_relevance": 0.95, "hook_potential": 0.9, "angle": "y", "search_queries": [], "avoid": True},
                {"i": 2, "niche_relevance": 0.9, "hook_potential": 0.8, "angle": "Why is the sky blue and not violet?",
                 "search_queries": ["Rayleigh scattering"], "avoid": False},
            ]})
        if "Using ONLY the source documents" in prompt:
            return json.dumps({"facts": FACTS, "supports_angle": True})
        if "RETENTION STRUCTURE" in prompt:
            return json.dumps(script_queue.pop(0))
        if "SCRIPT TO CHECK" in prompt:
            return json.dumps(check_queue.pop(0))
        raise AssertionError("unexpected prompt: " + prompt[:200])
    return respond


SUPPORTED = {"claims": [{"where": "beat 2", "claim": "blue scatters more", "fact_ids": ["F1"], "verdict": "supported"}]}
UNSUPPORTED = {"claims": [{"where": "hook", "claim": "sky is violet", "fact_ids": [], "verdict": "unsupported", "note": "soften"}]}


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    cfg = load_config()
    cfg.ledger_path = tmp_path / "ledger.json"
    state = RunState.create(cfg.runs_dir)

    def make(script_queue=None, check_queue=None, patch_queue=None):
        llm = LLM([(FakeProvider(responder(script_queue or [], check_queue or [], patch_queue)), 0)],
                  sleep=lambda s: None)
        return StageContext(run_dir=state.run_dir, config=cfg, state=state, _llm=llm)
    return make


@pytest.fixture
def mock_web(monkeypatch):
    def get_json(url, params=None):
        if params.get("list") == "search":
            return {"query": {"search": [{"title": "Rayleigh scattering"}]}}
        return {"query": {"pages": [{"extract": WIKI_TEXT}]}}
    monkeypatch.setattr(research.http, "get_json", get_json)
    monkeypatch.setattr(research.http, "get_text", lambda url, params=None: "<html></html>")


def test_script_validation_catches_problems(ctx):
    c = ctx()
    assert script.validate(GOOD_SCRIPT, c.config, {"F1", "F2", "F3"}) == []
    bad = json.loads(json.dumps(GOOD_SCRIPT))
    bad["hook"] = "In this video we will explain why the sky looks so very blue to everyone"
    bad["title"] = "X" * 80
    bad["beats"][1]["fact_ids"] = ["F7"]
    bad["beats"] = bad["beats"][:3]
    problems = script.validate(bad, c.config, {"F1", "F2", "F3"})
    joined = " | ".join(problems)
    for expected in ["hook has", "beats; need", "title is 80", "unknown fact ids", "banned phrase 'in this video'", "rehook"]:
        assert expected in joined, expected


def test_quote_verification():
    assert research.quote_in_source(FACTS[1]["quote"], WIKI_TEXT)
    assert not research.quote_in_source(FACTS[3]["quote"], WIKI_TEXT)


def test_rss_parsing(monkeypatch, ctx):
    rss = """<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
      <item><title>chandrayaan 4</title><ht:news_item><ht:news_item_title>ISRO plans</ht:news_item_title>
      <ht:news_item_url>https://example.com/isro</ht:news_item_url></ht:news_item></item>
      <item><title>ipl auction</title></item></channel></rss>"""
    monkeypatch.setattr(trends.http, "get_text", lambda url, params=None: rss)
    got = trends.collect_google_trends_rss(ctx().config)
    assert [c.title for c in got] == ["chandrayaan 4", "ipl auction"]
    assert got[0].related_urls == ["https://example.com/isro"] and got[0].popularity > got[1].popularity


def test_full_content_chain(ctx, mock_web, monkeypatch):
    monkeypatch.setattr(trends, "collect_all", lambda cfg, c=None: [
        trends.Candidate(title="IPL final tonight", source="google_trends", popularity=1.0),
        trends.Candidate(title="Why is the sky blue", source="reddit/r/askscience", popularity=0.5),
        trends.Candidate(title="Celebrity scandal", source="youtube", popularity=0.9),
    ])
    c = ctx(script_queue=[GOOD_SCRIPT], check_queue=[SUPPORTED])

    trends.run(c)
    topics = read_json(c.path("topics.json"))
    assert topics["selected"]["title"] == "Why is the sky blue"  # off-niche and avoid-flagged items dropped
    assert len(topics["candidates"]) == 1

    research.run(c)
    facts = read_json(c.path("facts.json"))["facts"]
    assert [f["id"] for f in facts] == ["F1", "F2", "F3"]  # hallucinated F9 dropped
    assert all(f["source_url"].startswith("https://en.wikipedia.org/wiki/") for f in facts)

    script.run(c)
    s = read_json(c.path("script.json"))
    assert 130 <= s["word_count"] <= 160
    assert "Sources:" in s["description"] and "wikipedia.org" in s["description"]

    factcheck.run(c)
    assert read_json(c.path("factcheck.json"))["passed"] is True


def test_script_retries_with_feedback(ctx, mock_web):
    bad = dict(GOOD_SCRIPT, title="Y" * 90)
    c = ctx(script_queue=[bad, GOOD_SCRIPT])
    (c.path("facts.json")).write_text(json.dumps({"topic": "sky", "angle": "why blue", "facts": VERIFIED}))
    script.run(c)
    prompts = c.llm._slots[0].provider.calls
    assert "title is 90 characters" in prompts[-1]


def test_factcheck_rewrites_then_aborts(ctx):
    c = ctx(script_queue=[GOOD_SCRIPT, GOOD_SCRIPT], check_queue=[UNSUPPORTED, UNSUPPORTED, UNSUPPORTED])
    c.path("facts.json").write_text(json.dumps({"topic": "sky", "angle": "why", "facts": VERIFIED}))
    c.path("script.json").write_text(json.dumps(GOOD_SCRIPT))
    with pytest.raises(AbortRun, match="after 2 rewrites"):
        factcheck.run(c)
    fc = read_json(c.path("factcheck.json"))
    assert fc["passed"] is False and len(fc["history"]) == 3


def test_factcheck_rewrite_then_pass(ctx):
    c = ctx(script_queue=[GOOD_SCRIPT], check_queue=[UNSUPPORTED, SUPPORTED])
    c.path("facts.json").write_text(json.dumps({"topic": "sky", "angle": "why", "facts": VERIFIED}))
    c.path("script.json").write_text(json.dumps(GOOD_SCRIPT))
    factcheck.run(c)
    assert read_json(c.path("factcheck.json"))["rewrites"] == 1
    assert c.path("script.v0.json").exists()


def test_research_moves_to_next_topic_then_aborts(ctx, monkeypatch):
    monkeypatch.setattr(research, "gather_sources", lambda cand, lang: [])
    c = ctx()
    c.path("topics.json").write_text(json.dumps({
        "candidates": [{"title": f"t{i}"} for i in range(5)], "selected": {"title": "t0"}, "rejected": []}))
    with pytest.raises(AbortRun):
        research.run(c)
    assert len(read_json(c.path("topics.json"))["rejected"]) == 3  # max_topic_attempts


def test_ledger_blocks_repeat_topic(ctx):
    c = ctx()
    Ledger(c.config.ledger_path).append({"topic": "Why is the sky blue and not violet?"})
    cand = trends.Candidate(title="sky blue", source="x", niche_relevance=0.9,
                            angle="Why is the sky blue and not violet", score=0.9)
    assert trends.rank([cand], Ledger(c.config.ledger_path), 0.7, 0.85) == []


def test_topics_from_aborted_runs_are_skipped(ctx):
    """Several runs a day see the same trends; a new run must not repeat an aborted run's dead end."""
    c = ctx()
    dead = RunState.create(c.config.runs_dir)
    (dead.run_dir / "topics.json").write_text(json.dumps({
        "candidates": [], "selected": {"title": "failed factcheck"}, "rejected": [{"title": "too few facts"}]}))
    dead.mark_aborted("factcheck", "claims unsupported")
    alive = RunState.create(c.config.runs_dir)  # unfinished runs don't count: they get resumed
    (alive.run_dir / "topics.json").write_text(json.dumps({"candidates": [], "selected": {"title": "in progress"}}))

    skip = trends.recently_failed_titles(c)
    assert skip == {"failed factcheck", "too few facts"}
    cands = [trends.Candidate(title=t, source="x", niche_relevance=0.9, score=s)
             for t, s in (("failed factcheck", 0.9), ("too few facts", 0.8), ("fresh topic", 0.5))]
    assert [x.title for x in trends.rank(cands, Ledger(c.config.ledger_path), 0.7, 0.85, skip)] == ["fresh topic"]


OVERSTATED = {"claims": [{"where": "beat 3", "claim": "about 5.5 times more", "fact_ids": ["F2"],
                          "verdict": "overstated", "note": "say roughly"}]}


def _factcheck_ctx(c):
    c.path("facts.json").write_text(json.dumps({"topic": "sky", "angle": "why", "facts": VERIFIED}))
    c.path("script.json").write_text(json.dumps(GOOD_SCRIPT))
    return c


def test_factcheck_patches_only_flagged_lines(ctx):
    patched = json.loads(json.dumps(GOOD_SCRIPT))
    patched["beats"][2]["narration"] = patched["beats"][2]["narration"].replace("about five", "roughly five")
    # empty script_queue: a full rewrite would crash, so passing proves the patch was used
    c = _factcheck_ctx(ctx(check_queue=[OVERSTATED, SUPPORTED], patch_queue=[patched]))
    factcheck.run(c)
    out = read_json(c.path("script.json"))
    assert "roughly five" in out["beats"][2]["narration"] and out["beats"][0] == GOOD_SCRIPT["beats"][0]
    assert out["word_count"] and out["description"]
    assert read_json(c.path("factcheck.json"))["rewrites"] == 1


def test_unusable_patch_falls_back_to_full_rewrite(ctx):
    # the flagged line (the hook) comes back far too long, so restoring passed lines can't save it
    long_hook = "This hook is now far too long to land within two seconds of the video"
    c = _factcheck_ctx(ctx(script_queue=[GOOD_SCRIPT], check_queue=[UNSUPPORTED, SUPPORTED],
                           patch_queue=[dict(GOOD_SCRIPT, hook=long_hook)] * 2))
    factcheck.run(c)
    prompts = c.llm._slots[0].provider.calls
    assert any("RETENTION STRUCTURE" in p for p in prompts)  # the full rewrite ran
    assert read_json(c.path("factcheck.json"))["passed"] is True


def test_patch_that_breaks_a_limit_gets_a_second_try(ctx):
    long_hook = "This hook is now far too long to land within two seconds of the video"
    c = _factcheck_ctx(ctx(check_queue=[UNSUPPORTED, SUPPORTED], patch_queue=[dict(GOOD_SCRIPT, hook=long_hook)]))
    factcheck.run(c)  # empty script_queue: a full rewrite would crash
    prompts = c.llm._slots[0].provider.calls
    assert any("YOUR LAST FIX BROKE THESE RULES" in p and "hook has" in p for p in prompts)
    assert not any("RETENTION STRUCTURE" in p for p in prompts)


def test_framing_needs_no_fact_id_but_numbers_do(ctx):
    c = ctx(check_queue=[
        {"claims": [
            {"where": "beat 1", "claim": "The secret starts miles away", "fact_ids": [], "verdict": "no_claim"},
            {"where": "hook", "claim": "the sky looks blue", "fact_ids": [], "verdict": "common_knowledge"},
            {"where": "beat 2", "claim": "blue scatters more", "fact_ids": ["F1"], "verdict": "supported"}]},
        {"claims": [{"where": "beat 3 on_screen_text", "claim": "5.5x more", "fact_ids": [], "verdict": "no_claim"}]},
        {"claims": [{"where": "beat 3", "claim": "about 9 times more", "fact_ids": [], "verdict": "common_knowledge"}]},
        {"claims": [{"where": "beat 2", "claim": "blue scatters more", "fact_ids": [], "verdict": "supported"}]},
    ])
    assert factcheck.check(c, VERIFIED, GOOD_SCRIPT)["passed"]
    assert factcheck.check(c, VERIFIED, GOOD_SCRIPT)["passed"]  # a number the facts state (F2: 5.5)
    numbers = factcheck.check(c, VERIFIED, GOOD_SCRIPT)
    assert not numbers["passed"] and "unsupported" in numbers["problems"][0]  # 9 is in no fact
    assert not factcheck.check(c, VERIFIED, GOOD_SCRIPT)["passed"]  # "supported" still needs a fact id


def test_reflag_of_an_approved_unchanged_line_is_ignored(ctx):
    """The checker can pass a line in one round and flag it, unchanged, in the next."""
    patched = json.loads(json.dumps(GOOD_SCRIPT))
    patched["beats"][2]["narration"] = patched["beats"][2]["narration"].replace("about five", "roughly five")
    reflag = {"claims": [{"where": "beat 2 narration", "claim": "Blue light gets scattered far more than red",
                          "fact_ids": [], "verdict": "unsupported", "note": "x"}]}
    c = _factcheck_ctx(ctx(check_queue=[OVERSTATED, reflag], patch_queue=[patched]))
    factcheck.run(c)
    fc = read_json(c.path("factcheck.json"))
    assert fc["passed"] and fc["rewrites"] == 1
    assert fc["history"][1]["claims"][0]["ignored"]


def test_patch_cannot_change_lines_that_passed(ctx):
    patched = json.loads(json.dumps(GOOD_SCRIPT))
    patched["beats"][2]["narration"] = patched["beats"][2]["narration"].replace("about five", "roughly five")
    patched["beats"][0]["narration"] += " Seriously."  # not flagged: must be put back
    patched["title"] = "A brand new title"
    c = _factcheck_ctx(ctx(check_queue=[OVERSTATED, SUPPORTED], patch_queue=[patched]))
    factcheck.run(c)
    out = read_json(c.path("script.json"))
    assert "roughly five" in out["beats"][2]["narration"]
    assert out["beats"][0]["narration"] == GOOD_SCRIPT["beats"][0]["narration"]
    assert out["title"] == GOOD_SCRIPT["title"]


def test_line_keys_map_checker_locations():
    assert script.line_keys("beat 5 on_screen_text") == ["beat 5 on_screen_text"]
    assert script.line_keys("Beat 2 narration") == ["beat 2 narration"]
    assert script.line_keys("beat 3") == ["beat 3 narration", "beat 3 on_screen_text", "beat 3 graphic"]
    assert script.line_keys("beat 2 graphic") == ["beat 2 graphic"]
    assert script.line_keys("title") == ["title"] and script.line_keys("overall tone") is None


def test_scorer_drops_reworded_repeats_of_covered_topics(ctx):
    """Fuzzy matching scores these two at 0.75 (< 0.85), so the model has to catch the repeat."""
    c = ctx()
    ledger = Ledger(c.config.ledger_path)
    ledger.append({"topic": "How do video games realistically simulate car crashes?"})
    seen = {}

    def respond(prompt):
        seen["prompt"] = prompt
        return json.dumps({"items": [
            {"i": 0, "niche_relevance": 0.9, "hook_potential": 0.9, "angle": "How does BeamNG fake a crash?", "covered": True},
            {"i": 1, "niche_relevance": 0.9, "hook_potential": 0.8, "angle": "Why does a cricket ball swing?"}]})

    c._llm = LLM([(FakeProvider(respond), 0)], sleep=lambda s: None)
    cands = [trends.Candidate(title="BeamNG crash test", source="youtube"), trends.Candidate(title="cricket", source="youtube")]
    out = trends.score_candidates(c, cands, trends.covered_topics(ledger))
    assert [x.title for x in out] == ["cricket"]
    assert "realistically simulate car crashes" in seen["prompt"]
