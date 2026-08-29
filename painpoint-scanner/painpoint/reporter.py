"""Stage 4: the weekly digest.

The report has two halves and they are kept strictly apart.

Everything countable -- mention counts, month-over-month change, which
workarounds came up how often, which competitors were named, and the
permalinks -- is assembled from the database. No model touches those numbers.

Only the narrative fields (the problem statement, why now, the app concept, the
riskiest assumption) are synthesized, and they are clearly the model's opinion.
Run with `--no-llm` and you still get a complete, useful report; you just lose
the prose.

The most important section of every entry is the list of threads at the end.
The model's job is to route your attention, not to make your decision.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .classifier import MAX_TOTAL_SCORE
from .config import Config
from .db import Database, parse_ts
from .queries import corpus_stats, get_cluster, top_by_vertical

log = logging.getLogger(__name__)

SYNTHESIS_SYSTEM = """\
You write one entry in a weekly digest of problems found on Reddit, for a small
software team deciding what to build next.

You are given a cluster of separate posts that describe the same underlying
problem, along with the workarounds and competing products people named.

Be concrete and skeptical. Describe the problem in the words the posters would
recognize. Do not inflate the opportunity, do not invent evidence, and do not
repeat the mention counts back -- the reader can already see them.

The app concept must be the thinnest version that actually solves the problem,
something a small team could ship in under eight weeks. If the cluster does not
support a buildable concept, say so plainly in that field.

The riskiest assumption is the single thing that, if wrong, kills this. Usually
it is that anyone will pay, that the described workflow is common rather than
idiosyncratic, or that the incumbents' gap is real rather than deliberate."""

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": {"type": "string"},
        "why_now": {"type": "string"},
        "app_concept": {"type": "string"},
        "riskiest_assumption": {"type": "string"},
        "competitor_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "complaint": {"type": "string"},
                },
                "required": ["name", "complaint"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "problem",
        "why_now",
        "app_concept",
        "riskiest_assumption",
        "competitor_notes",
    ],
    "additionalProperties": False,
}

FOOTER = """\
---

## Before you trust any of this

Reddit is not the market. It skews young, technical, English-speaking and
complaint-prone; a pain that is loud here may be quiet everywhere else, and
vice versa. Complaining is free and paying is not -- the money signal is the
best proxy in the rubric and it is still only a proxy. Nothing in this pipeline
is evidence that anyone will pay you.

Scores are a filter, not a verdict. This report exists to get you from thousands
of posts to five conversations.

**The next step is not more scraping.** DM ten people from the threads above and
ask what they tried and what they spend on it now. A week of that beats another
month of collection.
"""


def _fmt_change(this_month: int | None, prev_month: int | None) -> str:
    this_month, prev_month = this_month or 0, prev_month or 0
    if this_month == 0 and prev_month == 0:
        return "nothing in the last two months"
    if this_month == 0:
        return f"none in the last 30 days, {prev_month} the month before"
    if prev_month == 0:
        return f"{this_month} in the last 30 days, none the month before"
    direction = "up from" if this_month >= prev_month else "down from"
    return f"{this_month} in the last 30 days, {direction} {prev_month}"


def _fmt_tally(pairs: list[tuple[str, int]], limit: int = 5) -> str:
    if not pairs:
        return "_nothing recorded_"
    return "; ".join(
        f"{name} ({count})" if count > 1 else name for name, count in pairs[:limit]
    )


class Reporter:
    def __init__(self, db: Database, config: Config, client: Any | None = None):
        self.db = db
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    # -- synthesis ------------------------------------------------------

    def _synthesis_prompt(self, cluster: dict[str, Any]) -> str:
        members = cluster["members"][: self.config.reporter.threads_per_idea]
        excerpts = []
        for i, member in enumerate(members, 1):
            excerpts.append(
                f"{i}. r/{member.get('subreddit')} -- {member.get('title')}\n"
                f"   pain: {member.get('pain_summary')}\n"
                f"   quote: {member.get('evidence_quote') or '(none)'}\n"
                f"   workaround: {member.get('current_workaround') or '(none given)'}"
            )
        return "\n".join(
            [
                f"Cluster label: {cluster.get('canonical_pain')}",
                f"Vertical: {cluster.get('vertical')}",
                f"Posts in cluster: {cluster.get('member_count')}",
                f"Median rubric score: {cluster.get('median_score')}/{MAX_TOTAL_SCORE}",
                f"Workarounds named: {_fmt_tally(cluster.get('workarounds', []), 10)}",
                f"Competitors named: {_fmt_tally(cluster.get('competitors', []), 10)}",
                "",
                "Representative posts:",
                *excerpts,
            ]
        )

    def synthesize(self, cluster: dict[str, Any]) -> dict[str, Any]:
        response = self.client.messages.create(
            model=self.config.reporter.synthesis_model,
            max_tokens=16000,
            system=SYNTHESIS_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": SYNTHESIS_SCHEMA},
            },
            messages=[{"role": "user", "content": self._synthesis_prompt(cluster)}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text) if text else {}

    # -- rendering ------------------------------------------------------

    def render_cluster(self, cluster: dict[str, Any], narrative: dict[str, Any]) -> str:
        members = cluster["members"]
        threads = members[: self.config.reporter.threads_per_idea]

        competitor_notes = narrative.get("competitor_notes") or []
        if competitor_notes:
            competitors = "; ".join(
                f"**{n.get('name')}** -- {n.get('complaint')}" for n in competitor_notes
            )
        else:
            competitors = _fmt_tally(cluster.get("competitors", []))

        lines = [
            f"## {cluster.get('canonical_pain')}",
            "",
            f"Vertical: {cluster.get('vertical')}   "
            f"Mentions: {cluster.get('member_count')} "
            f"({_fmt_change(cluster.get('this_month_count'), cluster.get('prev_month_count'))})   "
            f"Score: {cluster.get('median_score')}/{MAX_TOTAL_SCORE}",
            "",
            f"**The problem:** {narrative.get('problem') or cluster.get('canonical_pain')}",
            "",
            f"**What people do now:** {_fmt_tally(cluster.get('workarounds', []))}",
            "",
            f"**Named competitors:** {competitors}",
            "",
            f"**Why now:** {narrative.get('why_now') or '_not synthesized_'}",
            "",
            f"**App concept:** {narrative.get('app_concept') or '_not synthesized_'}",
            "",
            f"**Riskiest assumption:** {narrative.get('riskiest_assumption') or '_not synthesized_'}",
            "",
            "**Go read these threads:**",
        ]

        for member in threads:
            created = parse_ts(member.get("created_utc"))
            when = f", {created:%Y-%m-%d}" if created else ""
            lines.append(
                f"- {member.get('permalink')} "
                f"({member.get('score')} points, {member.get('num_comments')} comments{when})"
            )
        if not threads:
            lines.append("- _no member posts_")

        return "\n".join(lines)

    def build(self, use_llm: bool = True) -> str:
        cfg = self.config.reporter
        grouped = top_by_vertical(self.db, cfg.top_n_per_vertical)
        stats = corpus_stats(self.db)

        sections = [
            f"# Pain point digest -- {date.today():%Y-%m-%d}",
            "",
            f"{stats['posts']} posts collected, {stats['passed_stage1']} passed the "
            f"stage 1 filter, {stats['classified']} classified, "
            f"{stats['painpoints']} judged real pain points, "
            f"{stats['clusters']} clusters.",
            "",
        ]

        if not grouped:
            sections.append(
                "No clusters yet. Run `painpoint collect`, `painpoint classify` and "
                "`painpoint cluster` first."
            )
            return "\n".join(sections)

        for vertical in sorted(grouped):
            sections.append(f"# {vertical}")
            sections.append("")
            for row in grouped[vertical]:
                cluster = get_cluster(self.db, row["id"])
                if not cluster:
                    continue

                narrative: dict[str, Any] = {}
                if use_llm:
                    try:
                        narrative = self.synthesize(cluster)
                    except Exception as exc:  # a failed entry must not kill the digest
                        log.warning(
                            "synthesis failed for cluster %s: %s", row["id"], exc
                        )
                        narrative = {}

                sections.append(self.render_cluster(cluster, narrative))
                sections.append("")

        sections.append(FOOTER)
        return "\n".join(sections)

    def write(self, out_dir: str | Path = "reports", use_llm: bool = True) -> Path:
        markdown = self.build(use_llm=use_llm)
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"digest-{date.today():%Y-%m-%d}.md"
        path.write_text(markdown)
        return path
