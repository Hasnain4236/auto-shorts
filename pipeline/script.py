"""Stage: script — a retention-engineered 45–60 s explainer built only from facts.json.

Retention structure (every element must stay TRUE and traceable to a fact):
  1. HOOK (≤ ~2 s): pattern-break. A counter-intuitive claim, a specific stakes
     statement, or a sharp question. Three variants are generated and the strongest kept.
  2. OPEN LOOP: an early beat promises a payoff ("the real reason is stranger") and
     the video withholds it until near the end.
  3. MECHANISM beats: one idea per beat, concrete everyday analogies, a striking number.
  4. RE-HOOK around the middle ("but here's the catch...") to stop the mid-video drop-off.
  5. PAYOFF closes the loop with the most surprising fact.
  6. LOOP ENDING + CTA: the last line flows back into the hook so replays feel seamless.
Format rules (word counts, hook length, title length, banned filler) are validated
in code, and the model is re-asked with the exact problems if any rule fails.

Output: script.json
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pipeline.config import Config
from pipeline.learn import HOOK_TYPES, choose_arm, playbook_prompt
from pipeline.llm import LLMError
from pipeline.stages import StageContext
from pipeline.state import atomic_write_json, read_json

log = logging.getLogger(__name__)

BANNED_PHRASES = [
    "in this video", "hey guys", "welcome back", "don't forget to like", "smash that",
    "let's dive in", "let's get started", "you won't believe", "hi everyone", "today we",
    "mind-blowing", "shocking", "subscribe now",
]

SYSTEM = (
    "You write scripts for a top-performing short-form science & tech explainer channel. "
    "Your scripts keep viewers watching to the last second, and you never state anything "
    "the provided facts do not support. You return JSON."
)

PROMPT = """Write a {min_s}–{max_s} second vertical short.

Topic: {topic}
Question the video answers: {angle}
Audience: {audience}
Tone: {tone}
Language: {language}

FACTS (the ONLY factual material you may use; cite their ids):
{facts}

RETENTION STRUCTURE (follow it):
1. hook: max {hook_words} words, spoken in under 2 seconds. It must open a CURIOSITY GAP:
   the viewer cannot guess the answer from the hook and needs to keep watching to close it.
   Never put the answer or a technical term in the hook. It should be about something the
   viewer has seen or felt, said in words a 12-year-old knows.
   Strong:  "Every firework is secretly a chemistry test."  "Your phone is lying about 100%."
            "This colour shouldn't exist, but you see it daily."
   Weak:    "Pyrotechnic colours come from excited electrons." (answer given away, jargon)
            "Why do firecrackers make sound?" (generic, no tension)
   No greeting, no "did you know", no channel name. First write 3 hook_options of different
   types, then put the strongest {hook_type} hook in "hook" (this video tests a "{hook_type}" hook).
2. Beat 1 opens a loop: it promises a payoff the viewer only gets near the end.
3. Middle beats explain the mechanism, one idea per beat. Use one vivid everyday analogy
   the audience instantly gets (phones, traffic, cooking, cricket, trains...). Include one
   striking number or comparison from the facts.
4. Around the middle, one "rehook" beat raises the stakes or flips expectations
   ("but here's the catch...").
5. The final beat pays off the loop with the most counter-intuitive fact.
6. cta: max 8 words, and its last words should flow naturally back into the hook so a
   replay feels seamless. Example pattern: "...and that's exactly why" -> (hook replays).

PLAIN LANGUAGE:
- Write for a smart 14-year-old with no science background. Use a technical word only if it
  is the payoff "name" of the thing, and explain it in the same sentence in everyday words.
- Prefer "the metal's atoms get hot and glow a colour" over "thermal excitation causes photoemission".
- Each beat should make the viewer picture something concrete.

SAFETY (hard rule): never give recipes, ingredient ratios, quantities or steps for making
explosives, weapons, drugs or dangerous chemicals, even if the facts contain them. Explain
the principle only ("it burns extremely fast"), never the formulation.

FORMAT RULES:
- {min_b}–{max_b} beats. Total spoken words (hook + all narration + cta): {min_w}–{max_w}.
- Short sentences, spoken style, second person ("you"). No filler, no hype words.
- on_screen_text: max {ost_words} words per beat; punchy keyword or number, not a copy of narration.
- visual_keywords: 2–3 search terms per beat for FREE STOCK VIDEO sites (Pexels/Pixabay).
  Use common, filmable subjects those sites actually have: "fireworks night sky",
  "hands holding smartphone", "scientist laboratory", "city traffic night", "ocean waves".
  2–3 words each, concrete nouns, no abstract ideas ("energy states", "regulations"),
  no names of real people or brands. Order from most to least specific.
- fact_ids: the fact ids each beat relies on. Every factual statement needs one.
- title: max 70 characters, curiosity-driven but literally true. No ALL CAPS, no "SHOCKING".
- Never use: {banned}.

Return JSON:
{{"hook_options": [{{"text": "...", "type": "counterintuitive|stakes|question"}}],
  "hook": "...",
  "beats": [{{"role": "open_loop|mechanism|rehook|payoff", "narration": "...",
              "on_screen_text": "...", "visual_keywords": ["..."], "fact_ids": ["F1"]}}],
  "cta": "...",
  "title": "...",
  "description_summary": "<2 sentences describing what the viewer learns>",
  "tags": ["<8–12 lowercase tags>"]}}
{playbook}
{feedback}"""

FEEDBACK = """
YOUR PREVIOUS DRAFT HAD PROBLEMS. Fix every one and return the full corrected script:
{problems}

Keep it just as gripping: fix only what is wrong, keep the curiosity-gap hook, the open
loop and the plain language. Do not turn it into a dull textbook paragraph. If a claim can't
be supported, replace it with a different supported fact rather than vaguer wording.

Previous draft:
{previous}"""


def word_count(text: str) -> int:
    return len(re.findall(r"[\w'’%-]+", text))


def spoken_text(script: dict[str, Any]) -> str:
    return " ".join([script.get("hook", "")] + [b.get("narration", "") for b in script.get("beats", [])]
                    + [script.get("cta", "")]).strip()


def validate(script: dict[str, Any], config: Config, fact_ids: set[str]) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    p: list[str] = []
    min_w, max_w = config.get("content.target_words", [130, 160])
    min_b, max_b = config.get("content.beats", [5, 7])
    hook_max = int(config.get("content.hook_max_words", 8))
    ost_max = int(config.get("content.on_screen_text_max_words", 5))

    hook = str(script.get("hook", "")).strip()
    beats = script.get("beats") or []
    if not hook:
        p.append("hook is missing")
    elif word_count(hook) > hook_max:
        p.append(f"hook has {word_count(hook)} words; max is {hook_max}")
    if not (min_b <= len(beats) <= max_b):
        p.append(f"{len(beats)} beats; need {min_b}–{max_b}")
    total = word_count(spoken_text(script))
    if not (min_w <= total <= max_w):
        p.append(f"total spoken words = {total}; need {min_w}–{max_w}")
    if not str(script.get("cta", "")).strip():
        p.append("cta is missing")
    elif word_count(script["cta"]) > 8:
        p.append("cta is longer than 8 words")

    title = str(script.get("title", "")).strip()
    if not title:
        p.append("title is missing")
    elif len(title) > 70:
        p.append(f"title is {len(title)} characters; max 70")
    elif title.isupper():
        p.append("title must not be ALL CAPS")

    roles = [b.get("role") for b in beats]
    if beats and "rehook" not in roles:
        p.append("no beat has role 'rehook'")
    if beats and roles[-1] != "payoff":
        p.append("the last beat must have role 'payoff'")

    for i, b in enumerate(beats, start=1):
        if not str(b.get("narration", "")).strip():
            p.append(f"beat {i} has no narration")
        if word_count(str(b.get("on_screen_text", ""))) > ost_max:
            p.append(f"beat {i} on_screen_text exceeds {ost_max} words")
        kws = b.get("visual_keywords") or []
        if not (1 <= len(kws) <= 3):
            p.append(f"beat {i} needs 1–3 visual_keywords")
        unknown = [f for f in b.get("fact_ids", []) if f not in fact_ids]
        if unknown:
            p.append(f"beat {i} cites unknown fact ids {unknown}")
        if b.get("role") in ("mechanism", "payoff") and not b.get("fact_ids"):
            p.append(f"beat {i} ({b.get('role')}) makes claims but cites no fact_ids")

    long_words = sorted({w for w in re.findall(r"[A-Za-z]{13,}", spoken_text(script))})
    if len(long_words) > 3:
        p.append(f"too much jargon for a general audience; simplify: {', '.join(long_words)}")

    lowered = (spoken_text(script) + " " + title).lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            p.append(f"uses banned phrase '{phrase}'")
    return p


def build_description(script: dict[str, Any], facts: list[dict[str, Any]]) -> str:
    """Description is assembled in code so every source is guaranteed to be listed."""
    seen: dict[str, str] = {}
    for f in facts:
        seen.setdefault(f["source_url"], f["source_title"])
    sources = "\n".join(f"• {title}: {url}" for url, title in seen.items())
    tags = " ".join("#" + re.sub(r"[^a-z0-9]", "", t.lower()) for t in script.get("tags", [])[:3] if t)
    return (
        f"{script.get('description_summary', '').strip()}\n\n"
        f"Sources:\n{sources}\n\n"
        "Script researched and written with AI assistance and fact-checked against the sources above. "
        "Voice is synthetic.\n\n"
        f"#shorts {tags}".strip()
    )


def _facts_block(facts: list[dict[str, Any]]) -> str:
    return "\n".join(f"{f['id']}: {f['fact']}" for f in facts)


PATCH_PROMPT = """A fact-checker reviewed this YouTube Short script. LINES FLAGGED BY THE FACT-CHECKER:
{problems}

FACTS (the only accepted evidence):
{facts}

SCRIPT (JSON):
{script}

Return the same JSON object, changing ONLY the flagged lines:
- Fix each flagged line with the smallest change that makes it true to the facts: correct or
  soften the wording, or swap in a different supported fact. Keep it spoken, punchy and plain.
- Copy every other field exactly as it is (hook, the other beats, roles, visual_keywords,
  title, tags, cta) unless a problem names it. Don't add new teasers, claims or numbers.
- If you change a beat, update its fact_ids.
- Keep the total spoken words (hook + narration + cta) between {min_w} and {max_w}."""

DERIVED_FIELDS = ("word_count", "hook_type", "description")


def script_lines(script: dict[str, Any]) -> dict[str, str]:
    """Every checkable line of a script, keyed the way the fact-checker names them."""
    lines = {k: str(script.get(k, "")) for k in ("title", "hook", "cta")}
    for i, b in enumerate(script.get("beats", []), start=1):
        lines[f"beat {i} narration"] = str(b.get("narration", ""))
        lines[f"beat {i} on_screen_text"] = str(b.get("on_screen_text", ""))
    return lines


def line_keys(where: str) -> list[str] | None:
    """The script line(s) a fact-checker "where" points at, or None if it can't be told."""
    w = (where or "").lower()
    m = re.search(r"beat\s*(\d+)", w)
    if m:
        n = m.group(1)
        if "screen" in w or ("text" in w and "narration" not in w):
            return [f"beat {n} on_screen_text"]
        if "narration" in w:
            return [f"beat {n} narration"]
        return [f"beat {n} narration", f"beat {n} on_screen_text"]
    return [k for k in ("title", "hook", "cta") if k in w] or None


def keep_lines(draft: dict[str, Any], original: dict[str, Any], keep: dict[str, str]) -> dict[str, Any]:
    """Copy the `keep` lines from the original into the draft (a kept narration brings its beat's
    fact ids and visuals along). Only when both have the same number of beats."""
    if len(draft.get("beats") or []) != len(original.get("beats") or []):
        return draft
    out = json.loads(json.dumps(draft))
    for key in keep:
        if key in ("title", "hook", "cta"):
            out[key] = original.get(key, "")
            continue
        m = re.fullmatch(r"beat (\d+) (narration|on_screen_text)", key)
        if not m:
            continue
        src, dst = original["beats"][int(m.group(1)) - 1], out["beats"][int(m.group(1)) - 1]
        fields = ("narration", "fact_ids", "visual_keywords", "role") if m.group(2) == "narration" else ("on_screen_text",)
        for f in fields:
            if f in src:
                dst[f] = src[f]
    return out


def _finalize(draft: dict[str, Any], facts: list[dict[str, Any]], hook_type: str | None) -> dict[str, Any]:
    draft["word_count"] = word_count(spoken_text(draft))
    draft["hook_type"] = hook_type
    draft["description"] = build_description(draft, facts)
    return draft


def patch_script(ctx: StageContext, facts_doc: dict[str, Any], script: dict[str, Any],
                 problems: list[str], keep: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Fix only the lines the fact-checker flagged. A full rewrite tends to fix one line and
    slip a new unsupported one in elsewhere; a patch converges. `keep` (lines that passed) is
    restored word for word, whatever the model returned. None if the patch is unusable."""
    cfg = ctx.config
    facts = facts_doc["facts"]
    min_w, max_w = cfg.get("content.target_words", [130, 160])
    editable = {k: v for k, v in script.items() if k not in DERIVED_FIELDS}
    prompt = PATCH_PROMPT.format(problems="\n".join(f"- {p}" for p in problems), facts=_facts_block(facts),
                                 script=json.dumps(editable, ensure_ascii=False, indent=1), min_w=min_w, max_w=max_w)
    try:
        draft = ctx.llm_for("writer").complete_json(prompt, system=SYSTEM, temperature=0.3)
    except LLMError as e:
        log.warning("script: patch failed (%s); rewriting instead", e)
        return None
    if not isinstance(draft, dict):
        return None
    ids = {f["id"] for f in facts}
    # Prefer the patch with every passed line restored; if that breaks the word count, take it as returned.
    for candidate in ([keep_lines(draft, script, keep)] if keep else []) + [draft]:
        issues = validate(candidate, cfg, ids)
        if not issues:
            draft = candidate
            break
    else:
        log.warning("script: patch invalid (%s); rewriting instead", "; ".join(issues))
        return None
    hook_type = script.get("hook_type") or ctx.state.data.get("experiment", {}).get("hook_type")
    draft = _finalize(draft, facts, hook_type)
    log.info("script: patched %d flagged line(s): '%s' (%d words)", len(problems), draft["title"], draft["word_count"])
    return draft


def write_script(
    ctx: StageContext,
    facts_doc: dict[str, Any],
    *,
    problems: list[str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a script, re-asking with concrete problems until it passes validation."""
    cfg = ctx.config
    facts = facts_doc["facts"]
    fact_ids = {f["id"] for f in facts}
    min_s, max_s = cfg.get("video.duration_s", [45, 60])
    min_w, max_w = cfg.get("content.target_words", [130, 160])
    min_b, max_b = cfg.get("content.beats", [5, 7])
    attempts = int(cfg.get("content.max_script_attempts", 3))

    last: dict[str, Any] | None = previous
    issues = problems or []
    # Experiment: which hook style performs best on this channel (see pipeline/learn.py).
    experiment = ctx.state.data.setdefault("experiment", {})
    if "hook_type" not in experiment:
        experiment["hook_type"] = choose_arm(cfg, "hook_type", HOOK_TYPES)
        ctx.state.save()
    hook_type = experiment["hook_type"]
    for attempt in range(1, attempts + 1):
        feedback = (
            FEEDBACK.format(problems="\n".join(f"- {x}" for x in issues),
                            previous=json.dumps(last, ensure_ascii=False, indent=1))
            if issues and last else ""
        )
        prompt = PROMPT.format(
            min_s=min_s, max_s=max_s, topic=facts_doc["topic"], angle=facts_doc.get("angle") or facts_doc["topic"],
            audience=cfg.get("channel.audience"), tone=cfg.get("channel.tone"),
            language=cfg.get("channel.language", "en"), facts=_facts_block(facts),
            hook_words=cfg.get("content.hook_max_words", 8), min_b=min_b, max_b=max_b,
            min_w=min_w, max_w=max_w, ost_words=cfg.get("content.on_screen_text_max_words", 5),
            banned=", ".join(f'"{b}"' for b in BANNED_PHRASES), feedback=feedback,
            hook_type=hook_type, playbook=playbook_prompt(cfg, "script"),
        )
        draft = ctx.llm_for("writer").complete_json(prompt, system=SYSTEM, temperature=0.8)
        if not isinstance(draft, dict):
            issues, last = ["response was not a JSON object"], {}
            continue
        issues = validate(draft, cfg, fact_ids)
        if not issues:
            draft = _finalize(draft, facts, hook_type)
            log.info("script: '%s' (%d words, %d beats, attempt %d)",
                     draft["title"], draft["word_count"], len(draft["beats"]), attempt)
            return draft
        log.warning("script: attempt %d invalid: %s", attempt, "; ".join(issues))
        last = draft
    raise LLMError(f"Script failed validation after {attempts} attempts: {'; '.join(issues)}")


def run(ctx: StageContext) -> list[Path]:
    facts_doc = read_json(ctx.path("facts.json"))
    script = write_script(ctx, facts_doc)
    out = ctx.path("script.json")
    atomic_write_json(out, script)
    return [out]
