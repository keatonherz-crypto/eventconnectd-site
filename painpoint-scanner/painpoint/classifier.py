"""Stage 2: the paid filter.

Only posts that survived stage 1 reach this module. Classification is a
labelling job rather than a reasoning job, so it runs on Haiku in batches of 20.

Two deliberate choices:

* **The model never computes the total.** It returns five sub-scores and this
  module adds them up after clamping each to 0-5. Arithmetic is not what you
  are paying a model for, and a hallucinated total silently corrupts every
  ranking downstream.
* **A post is scored once, ever.** The insert is ON CONFLICT DO NOTHING, so
  re-running the classifier costs nothing on already-seen posts and a crashed
  run can simply be restarted.

Before trusting any of this, hand-check 50 classifications against your own
judgment (`painpoint classify --review 50`). Your calibration is the ground
truth here, not the model's.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import Config
from .db import Database, ts_param, utcnow

log = logging.getLogger(__name__)

SCORE_DIMENSIONS = (
    "specificity",
    "workflow_frequency",
    "money_signal",
    "incumbent_gap",
    "buildability",
)
MAX_TOTAL_SCORE = 5 * len(SCORE_DIMENSIONS)

SYSTEM_PROMPT = """\
You classify Reddit posts to find real, unsolved problems that software could fix.

For each post, return JSON only. No preamble, no markdown fences.

A post is a pain point ONLY if a person describes a specific recurring problem
they personally experience. It is NOT a pain point if it is a feature request
for an existing product, a support question with a known answer, a rant with no
describable process behind it, or someone promoting their own tool.

pain_summary must describe the PROBLEM, not the app. Write "I lose track of
which invoices I sent", not "needs an invoice tracker". The same problem gets
proposed with ten different solutions, and summaries that describe solutions
will not cluster together.

Score each post on five dimensions, 0-5:

specificity: How concretely is the problem described? A named workflow with
steps scores 5. "Everything is broken" scores 0.

workflow_frequency: A daily task scores 5, weekly 3, once a year 1. People do
not pay for annual annoyances.

money_signal: The strongest signal. Paying for a tool that does not work, or
paying a person to do it manually, scores 5. A quantified hourly time cost, or
using a paid tool in a way it was not designed for, scores 4. Wanting something
free scores 0 and is a red flag.

incumbent_gap: 5 when incumbents exist AND people complain about them in the
thread -- validated demand plus an opening. 3 when incumbents exist but are
enterprise-priced or serve an adjacent segment. 2 when no incumbent is
mentioned: unproven, not a win. 0 when a well-loved product already owns this.

buildability: Can a small team ship a usable v1 in under 8 weeks? Score down
hard for anything needing hardware, day-one marketplace liquidity, an
integration partner who has to say yes, regulatory approval, or data you cannot
legally get.

Name any competing product mentioned in the post or its comments, in
competitors. A named competitor is the search term for the next round of
listening, so record it even in passing.

Return one object per post, with the same id you were given:
{
  "id": "<post id>",
  "is_painpoint": true|false,
  "pain_summary": "<one sentence, present tense, describes the PROBLEM>",
  "vertical": "<short label>",
  "current_workaround": "<what they say they do now, or null>",
  "competitors": ["<named product>", ...],
  "scores": {
    "specificity": 0-5,
    "workflow_frequency": 0-5,
    "money_signal": 0-5,
    "incumbent_gap": 0-5,
    "buildability": 0-5
  },
  "evidence_quote": "<under 15 words from the post, or null>"
}

Return {"results": [...]} containing one object for every post, in order."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "is_painpoint": {"type": "boolean"},
                    "pain_summary": {"type": ["string", "null"]},
                    "vertical": {"type": ["string", "null"]},
                    "current_workaround": {"type": ["string", "null"]},
                    "competitors": {"type": "array", "items": {"type": "string"}},
                    "scores": {
                        "type": "object",
                        "properties": {d: {"type": "integer"} for d in SCORE_DIMENSIONS},
                        "required": list(SCORE_DIMENSIONS),
                        "additionalProperties": False,
                    },
                    "evidence_quote": {"type": ["string", "null"]},
                },
                "required": [
                    "id",
                    "is_painpoint",
                    "pain_summary",
                    "vertical",
                    "current_workaround",
                    "competitors",
                    "scores",
                    "evidence_quote",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

PENDING_SQL = """
SELECT p.id, p.subreddit, p.vertical, p.title, p.body, p.score, p.num_comments
FROM posts p
LEFT JOIN classifications c ON c.post_id = p.id
WHERE p.stage1_keep = 1 AND c.post_id IS NULL
ORDER BY p.created_utc DESC
"""

INSERT_CLASSIFICATION = """
INSERT INTO classifications (
  post_id, is_painpoint, pain_summary, vertical, current_workaround,
  evidence_quote, competitors, scores, total_score, model, classified_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (post_id) DO NOTHING
"""

TOP_COMMENTS_SQL = """
SELECT body FROM comments WHERE post_id = ? ORDER BY score DESC LIMIT ?
"""


@dataclass
class ClassifierStats:
    batches: int = 0
    posts_sent: int = 0
    written: int = 0
    painpoints: int = 0
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"batches:      {self.batches}",
            f"posts sent:   {self.posts_sent}",
            f"written:      {self.written}",
            f"pain points:  {self.painpoints}",
        ]
        if self.missing:
            lines.append(f"no result returned for {len(self.missing)} posts (will retry)")
        if self.errors:
            lines.append(f"errors ({len(self.errors)}):")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)


def clamp_scores(raw: Any) -> dict[str, int]:
    """Coerce the model's score object into five integers in 0-5."""
    values = raw if isinstance(raw, dict) else {}
    scores: dict[str, int] = {}
    for dim in SCORE_DIMENSIONS:
        try:
            value = int(round(float(values.get(dim, 0))))
        except (TypeError, ValueError):
            value = 0
        scores[dim] = max(0, min(5, value))
    return scores


def total_score(scores: dict[str, int]) -> int:
    return sum(scores.get(dim, 0) for dim in SCORE_DIMENSIONS)


def extract_json(text: str) -> dict[str, Any]:
    """Parse the model's reply, tolerating fences when structured output is off."""
    stripped = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    try:
        return json.loads(stripped)
    except ValueError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


class Classifier:
    def __init__(self, db: Database, config: Config, client: Any | None = None):
        self.db = db
        self.config = config
        self._client = client
        # Flipped off permanently if the endpoint rejects output_config, so a
        # model without structured-output support degrades to fenced JSON
        # instead of failing every batch.
        self.structured_output = True

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def pending_posts(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = PENDING_SQL + ("\nLIMIT ?" if limit else "")
        return self.db.query(sql, (limit,) if limit else ())

    def run(self, limit: int | None = None) -> ClassifierStats:
        posts = self.pending_posts(limit)
        stats = ClassifierStats()
        batch_size = max(1, self.config.classifier.batch_size)

        for start in range(0, len(posts), batch_size):
            batch = posts[start : start + batch_size]
            stats.batches += 1
            stats.posts_sent += len(batch)
            try:
                results = self.classify_batch(batch)
            except Exception as exc:
                log.warning("batch %d failed: %s", stats.batches, exc)
                stats.errors.append(str(exc))
                continue

            by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
            for post in batch:
                result = by_id.get(post["id"])
                if result is None:
                    stats.missing.append(post["id"])
                    continue
                written, is_pain = self._store(post, result)
                stats.written += int(written)
                stats.painpoints += int(written and is_pain)
            self.db.commit()

        if stats.missing:
            log.info("no classification returned for: %s", ", ".join(stats.missing[:20]))
        return stats

    # -- prompt ---------------------------------------------------------

    def _comments_for(self, post_id: str, limit: int = 2) -> list[str]:
        rows = self.db.query(TOP_COMMENTS_SQL, (post_id, limit))
        return [r["body"] for r in rows if r.get("body")]

    def render_post(self, post: dict[str, Any]) -> str:
        max_chars = self.config.classifier.max_body_chars
        body = (post.get("body") or "")[:max_chars]
        parts = [
            f"id: {post['id']}",
            f"subreddit: r/{post.get('subreddit', '')}",
            f"score: {post.get('score', 0)}  comments: {post.get('num_comments', 0)}",
            f"title: {post.get('title', '')}",
            f"body: {body}",
        ]
        # Competitors and workarounds usually surface in the replies, not the
        # post, so a couple of top comments materially improve incumbent_gap.
        for i, comment in enumerate(self._comments_for(post["id"]), 1):
            parts.append(f"top comment {i}: {comment[:400]}")
        return "\n".join(parts)

    def build_prompt(self, posts: Sequence[dict[str, Any]]) -> str:
        blocks = "\n\n---\n\n".join(self.render_post(p) for p in posts)
        return (
            f"Classify these {len(posts)} Reddit posts. Return one result object "
            f"for each, using the exact id given.\n\n{blocks}"
        )

    # -- model call -----------------------------------------------------

    def classify_batch(self, posts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        import anthropic

        request: dict[str, Any] = {
            "model": self.config.classifier.model,
            "max_tokens": 8000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": self.build_prompt(posts)}],
        }
        if self.structured_output:
            request["output_config"] = {
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA}
            }

        try:
            response = self.client.messages.create(**request)
        except anthropic.BadRequestError as exc:
            if not self.structured_output:
                raise
            # Most likely the model does not support output_config. Drop it for
            # the rest of the run rather than failing every remaining batch.
            log.warning("structured output rejected (%s); falling back to raw JSON", exc)
            self.structured_output = False
            request.pop("output_config", None)
            response = self.client.messages.create(**request)

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise RuntimeError(f"empty response (stop_reason={response.stop_reason})")

        payload = extract_json(text)
        results = payload.get("results", payload if isinstance(payload, list) else [])
        return list(results) if isinstance(results, list) else []

    # -- persistence ----------------------------------------------------

    def _store(self, post: dict[str, Any], result: dict[str, Any]) -> tuple[bool, bool]:
        scores = clamp_scores(result.get("scores"))
        is_pain = bool(result.get("is_painpoint"))
        competitors = [
            str(c) for c in (result.get("competitors") or []) if isinstance(c, (str, int))
        ]

        self.db.execute(
            INSERT_CLASSIFICATION,
            (
                post["id"],
                1 if is_pain else 0,
                result.get("pain_summary"),
                result.get("vertical") or post.get("vertical"),
                result.get("current_workaround"),
                result.get("evidence_quote"),
                self.db.json_param(competitors),
                self.db.json_param(scores),
                total_score(scores),
                self.config.classifier.model,
                ts_param(utcnow()),
            ),
        )
        return True, is_pain


REVIEW_SQL = """
SELECT p.title, p.permalink, p.body, c.is_painpoint, c.pain_summary,
       c.current_workaround, c.evidence_quote, c.scores, c.total_score
FROM classifications c JOIN posts p ON p.id = c.post_id
ORDER BY c.classified_at DESC
LIMIT ?
"""


def review_sample(db: Database, limit: int = 50) -> str:
    """Print recent classifications for hand-checking against your own judgment."""
    rows = db.query(REVIEW_SQL, (limit,))
    if not rows:
        return "No classifications yet."

    out: list[str] = []
    for i, row in enumerate(rows, 1):
        scores = Database.load_json(row.get("scores"), {}) or {}
        verdict = "PAIN" if row.get("is_painpoint") else "not a pain point"
        out.append(
            f"[{i}] {verdict}  total={row.get('total_score')}/{MAX_TOTAL_SCORE}\n"
            f"    title:      {row.get('title')}\n"
            f"    summary:    {row.get('pain_summary')}\n"
            f"    workaround: {row.get('current_workaround')}\n"
            f"    quote:      {row.get('evidence_quote')}\n"
            f"    scores:     "
            + ", ".join(f"{d}={scores.get(d)}" for d in SCORE_DIMENSIONS)
            + f"\n    {row.get('permalink')}"
        )
    out.append(
        f"\nAgree with these {len(rows)}? If not, fix the rubric wording in "
        "SYSTEM_PROMPT before building anything on top of the scores."
    )
    return "\n\n".join(out)
