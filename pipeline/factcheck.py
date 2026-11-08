"""Stage: factcheck — independent second pass mapping every claim to a fact.

A separate low-temperature LLM call (the checker never sees the writer's reasoning)
lists every factual claim in the hook, narration, on-screen text and title, and maps
each to the fact id(s) that support it (framing and everyday knowledge need none, unless they
carry a number no fact states). Any unsupported or overstated claim triggers a patch of just
the flagged lines (or a full rewrite if the patch is unusable), using the checker's notes.
Lines that passed stay passed while unchanged: the checker isn't consistent between rounds,
and re-judging every line each round made rewrites chase new, random flags. After
`content.max_factcheck_rewrites` failed rewrites the run is aborted (and I'm notified).

Outputs: factcheck.json, and script.json is replaced by the approved version
(the original draft is kept as script.v0.json).
"""
from __future__ import annotations

import json
import logging
import re
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
1. List EVERY statement in the title, hook, narration, on-screen text and cta, including
   numbers, comparisons, superlatives ("the fastest", "the only") and causal claims.
   Analogies are fine if clearly framed as analogies and not misleading.
2. For each, give the supporting fact ids, or none.
3. verdict, one of:
   "supported"        a fact states it, or it follows directly from the facts (needs fact ids).
                      Plain-language paraphrases and simplifications that keep the meaning are
                      supported. Never penalise the script for avoiding the source's technical
                      wording: only flag claims whose meaning goes beyond or contradicts the facts.
   "common_knowledge" a universally known everyday observation (fireworks are loud, the sky
                      looks blue, phones get warm). No fact id needed.
   "no_claim"         framing that asserts no checkable fact: the question the video asks, teasers
                      and transitions ("here's the twist", "the secret starts miles away", "stay
                      with me"), or a title/cta that only names or restates the topic. No fact id needed.
   "overstated"       a fact exists but the script exaggerates it (a rounded-up number, "always"
                      vs "often", or a clickbait title that over-promises).
   "unsupported"      a specific mechanism, number, cause, date, name or superlative that no fact
                      states. A specific claim is never common_knowledge or no_claim.
   "unsafe"           recipes, ingredient ratios, quantities or steps for making explosives,
                      weapons, drugs or dangerous chemicals, even if a fact supports it.
   In "note", suggest a fix in plain everyday language, never more technical wording.

Return JSON: {{"claims": [{{"where": "hook|beat 1|title|cta|...", "claim": "...", "fact_ids": ["F1"],
"verdict": "supported|common_knowledge|no_claim|overstated|unsupported|unsafe", "note": "<what to fix>"}}]}}"""

# Accepted without a fact id, as long as any number in them is one the facts state.
FREE_VERDICTS = ("common_knowledge", "no_claim")


def _beats_block(script: dict[str, Any]) -> str:
    return "\n".join(
        f"beat {i}: narration: {b.get('narration', '')} | on_screen_text: {b.get('on_screen_text', '')}"
        for i, b in enumerate(script.get("beats", []), start=1)
    )


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,.]*", text)}


def _problem(c: dict[str, Any]) -> str:
    return f"[{c.get('where')}] {c.get('verdict')}: \"{c.get('claim')}\" — {c.get('note') or 'remove or support it'}"


def _finish(result: dict[str, Any], flagged: list[dict[str, Any]]) -> dict[str, Any]:
    result.update(flagged=flagged, problems=[_problem(c) for c in flagged],
                  passed=not flagged and bool(result["claims"]))
    return result


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
    stated_numbers = set().union(*(_numbers(f["fact"]) for f in facts))
    claims = result.get("claims", []) if isinstance(result, dict) else []
    for c in claims:
        verdict = c.get("verdict")
        cited = [f for f in c.get("fact_ids") or [] if f in known]
        if verdict == "supported" and not cited:
            verdict = "unsupported"  # "supported" with no valid fact id is not accepted
        elif verdict in FREE_VERDICTS and not _numbers(str(c.get("claim", ""))) <= stated_numbers:
            verdict = "unsupported"  # framing can't carry a number the facts don't state
            c["note"] = c.get("note") or "use only numbers the facts state"
        c["verdict"] = verdict
    return _finish({"claims": claims}, [c for c in claims if c["verdict"] not in ("supported", *FREE_VERDICTS)])


def _drop_reflags(result: dict[str, Any], script: dict[str, Any], approved: dict[str, str]) -> None:
    """The checker isn't consistent between rounds: it can pass a line and then flag the same,
    unchanged line next round. A line that passed and hasn't changed since stays passed
    (an "unsafe" verdict always counts)."""
    if not approved:
        return
    lines = script_mod.script_lines(script)
    kept = []
    for c in result["flagged"]:
        keys = script_mod.line_keys(str(c.get("where", "")))
        if c.get("verdict") != "unsafe" and keys and all(approved.get(k) == lines.get(k) for k in keys):
            c["ignored"] = "passed in an earlier round, unchanged since"
            log.info("factcheck: ignoring a re-flag of an approved, unchanged line [%s] %s", c.get("where"), c.get("claim"))
            continue
        kept.append(c)
    _finish(result, kept)


def _approve(result: dict[str, Any], script: dict[str, Any], approved: dict[str, str]) -> dict[str, str]:
    """Remember the lines this round passed; returns them so a patch keeps them word for word."""
    keys = [script_mod.line_keys(str(c.get("where", ""))) for c in result["flagged"]]
    if not result["claims"] or any(k is None for k in keys):
        return {}  # nothing checked, or a flag we can't place on a line
    bad = {k for ks in keys for k in ks}
    for k in bad:
        approved.pop(k, None)
    passed = {k: v for k, v in script_mod.script_lines(script).items() if k not in bad}
    approved.update(passed)
    return passed


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
    approved: dict[str, str] = {}  # line -> the text that passed
    for rnd in range(max_rewrites + 1):
        result = check(ctx, facts, script)
        _drop_reflags(result, script, approved)
        history.append({"round": rnd, "title": script.get("title"),
                        **{k: v for k, v in result.items() if k != "flagged"}})
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
        keep = _approve(result, script, approved)
        if rnd == max_rewrites:
            break
        # Patch just the flagged lines; fall back to a full rewrite if the patch is unusable.
        patched = (script_mod.patch_script(ctx, facts_doc, script, result["problems"], keep=keep)
                   if result["claims"] else None)
        script = patched or script_mod.write_script(ctx, facts_doc, problems=result["problems"], previous=script)

    atomic_write_json(ctx.path("factcheck.json"), {"passed": False, "rewrites": max_rewrites, "history": history})
    raise AbortRun(
        f"Fact-check failed after {max_rewrites} rewrites. Last problems: "
        + json.dumps(history[-1]["problems"][:3], ensure_ascii=False)
    )
