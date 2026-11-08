"""Stage: factcheck — independent second pass mapping every claim to a fact.

A separate low-temperature LLM call (the checker never sees the writer's reasoning)
lists every factual claim in the hook, narration, on-screen text and title, and maps
each to the fact id(s) that support it. Any unsupported or overstated claim triggers
a rewrite via the script writer with the checker's notes. After
`content.max_factcheck_rewrites` failed rewrites the run is aborted (and I'm notified).

Outputs: factcheck.json, and script.json is replaced by the approved version
(the original draft is kept as script.v0.json).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline import script as script_mod
from pipeline.stages import AbortRun, StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a strict fact-checker for an educational science channel. You assume nothing. "
    "A claim is supported only if the listed facts state it or it follows directly from them. "
    "You return JSON."
)

PROMPT = """FACTS (the only accepted evidence):
{facts}

SCRIPT TO CHECK:
title: {title}
hook: {hook}
{beats}
cta: {cta}

Instructions:
1. List EVERY factual claim in the title, hook, narration and on-screen text, including
   numbers, comparisons, superlatives ("the fastest", "the only") and causal claims.
   Analogies are fine if clearly framed as analogies and not misleading.
   Universally known everyday observations (fireworks are loud, the sky looks blue, phones
   get warm) and the framing of the question itself are not claims that need a fact id;
   mark them "supported". Flag specific mechanisms, numbers, causes, history and superlatives.
   Plain-language paraphrases and simplifications that keep the meaning correct are
   SUPPORTED. Do not penalise the script for avoiding the source's technical wording;
   only flag claims whose meaning goes beyond or contradicts the facts.
   In "note", suggest a fix in plain everyday language, never more technical wording.
2. For each claim give the supporting fact ids, or none.
3. verdict: "supported" | "overstated" (fact exists but the script exaggerates it, e.g. a
   rounded-up number, "always" vs "often", or a clickbait title that over-promises) | "unsupported"
   | "unsafe" (gives recipes, ingredient ratios, quantities or steps for making explosives,
   weapons, drugs or dangerous chemicals, even if a fact supports it).

Return JSON: {{"claims": [{{"where": "hook|beat 1|title|...", "claim": "...",
"fact_ids": ["F1"], "verdict": "supported|overstated|unsupported|unsafe", "note": "<what to fix>"}}]}}"""


def _beats_block(script: dict[str, Any]) -> str:
    return "\n".join(
        f"beat {i}: narration: {b.get('narration', '')} | on_screen_text: {b.get('on_screen_text', '')}"
        for i, b in enumerate(script.get("beats", []), start=1)
    )


def check(ctx: StageContext, facts: list[dict[str, Any]], script: dict[str, Any]) -> dict[str, Any]:
    result = ctx.llm_for("checker").complete_json(
        PROMPT.format(
            facts="\n".join(f"{f['id']}: {f['fact']}  (quote: \"{f['quote']}\")" for f in facts),
            title=script.get("title", ""), hook=script.get("hook", ""),
            beats=_beats_block(script), cta=script.get("cta", ""),
        ),
        system=SYSTEM, temperature=0.1,
    )
    known = {f["id"] for f in facts}
    claims = result.get("claims", []) if isinstance(result, dict) else []
    problems: list[str] = []
    for c in claims:
        verdict = c.get("verdict")
        cited = [f for f in c.get("fact_ids") or [] if f in known]
        if verdict == "supported" and not cited:
            verdict = "unsupported"  # "supported" with no valid fact id is not accepted
            c["verdict"] = verdict
        if verdict != "supported":
            problems.append(f"[{c.get('where')}] {verdict}: \"{c.get('claim')}\" — {c.get('note') or 'remove or support it'}")
    return {"claims": claims, "problems": problems, "passed": not problems and bool(claims)}


def run(ctx: StageContext) -> list[Path]:
    cfg = ctx.config
    facts_doc = read_json(ctx.path("facts.json"))
    facts = facts_doc["facts"]
    script_path = ctx.path("script.json")
    script = read_json(script_path)
    max_rewrites = int(cfg.get("content.max_factcheck_rewrites", 2))

    v0 = ctx.path("script.v0.json")
    if not v0.exists():
        atomic_write_json(v0, script)

    history: list[dict[str, Any]] = []
    for rnd in range(max_rewrites + 1):
        result = check(ctx, facts, script)
        history.append({"round": rnd, "title": script.get("title"), **result})
        if result["passed"]:
            log.info("factcheck: passed (%d claims, round %d)", len(result["claims"]), rnd)
            atomic_write_json(script_path, script)
            out = ctx.path("factcheck.json")
            atomic_write_json(out, {"passed": True, "rewrites": rnd, "history": history})
            return [out, script_path]
        if not result["claims"]:
            result["problems"] = ["checker found no claims; script may be empty or unparseable"]
        log.warning("factcheck round %d: %d problem(s):\n  %s", rnd, len(result["problems"]),
                    "\n  ".join(result["problems"]))
        if rnd == max_rewrites:
            break
        script = script_mod.write_script(ctx, facts_doc, problems=result["problems"], previous=script)

    atomic_write_json(ctx.path("factcheck.json"), {"passed": False, "rewrites": max_rewrites, "history": history})
    raise AbortRun(
        f"Fact-check failed after {max_rewrites} rewrites. Last problems: "
        + json.dumps(history[-1]["problems"][:3], ensure_ascii=False)
    )
