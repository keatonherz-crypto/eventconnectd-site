"""Stage 1: the free filter.

This runs on every post the collector sees and is meant to kill roughly 90% of
volume before anything reaches the model. Only what survives here costs money.

Rules come in two strengths:

* **Hard** rules drop a post outright. A bot, a self-promo launch post, or a
  body with nothing in it cannot become interesting no matter how it is worded.
* **Soft** rules drop a post unless it contains first-person struggle language.
  Low engagement and lookup-shaped questions are usually noise, but "nobody
  upvoted it" is not the same as "nobody has this problem" -- a detailed
  account of a daily grind that landed at zero points is exactly the kind of
  thing this pipeline exists to find.

`summarize()` exists for tuning: run a week of collected posts through the
filter and look at the survival rate and the drop reasons before you trust it.
Aim for about 10% surviving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Bodies shorter than this have no process in them to build against.
MIN_BODY_CHARS = 100
# "Nobody else cared" threshold, only applied when there is also no discussion.
MIN_SCORE = 3


def _rx(*patterns: str) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


STRUGGLE_PATTERNS = _rx(
    r"\bi have to\b",
    r"\bi still have to\b",
    r"\bevery time i\b",
    r"\bevery single (time|day|week)\b",
    r"\bi end up\b",
    r"\bi(?:'ve| have) tried\b",
    r"\bwe currently\b",
    r"\bi currently\b",
    r"\bi keep having to\b",
    r"\bi spend (hours|\d+)\b",
    r"\btakes me (hours|forever|\d+)\b",
    r"\bby hand\b",
    r"\bmanually\b",
    r"\bi'?m stuck\b",
    r"\bdriving me (crazy|nuts|insane)\b",
    r"\bwish there was\b",
    r"\bthere has to be a better way\b",
)

SELF_PROMO_PATTERNS = _rx(
    r"\bcheck out my\b",
    r"\bjust launched\b",
    r"\blaunching my\b",
    r"\bintroducing my\b",
    r"\bmy new (app|tool|product|saas|startup|site)\b",
    r"\bfeedback on my (app|tool|product|saas|startup|site|mvp|landing)\b",
    r"\bwe just (released|shipped|launched)\b",
    r"\bshameless plug\b",
    r"\blink in (bio|comments)\b",
    r"\bpromo code\b",
    r"\bsign ?up (at|here)\b",
    r"\b(waitlist|beta) sign-?up\b",
    r"\bdm me (for|if you)\b",
)

MEME_PATTERNS = _rx(
    r"^\s*\[?(meme|shitpost|oc|humou?r)\]?\b",
    r"\bmeme monday\b",
    r"\bfound this (gem|beauty|in the wild)\b",
    r"\bjust for (fun|laughs)\b",
    r"^\s*(lol|lmao|rofl)\b",
    r"\bcursed\b",
)

# Deliberately narrow: this is a soft rule, and over-matching here throws away
# real posts that merely happen to open with a question.
FACTUAL_QUESTION_PATTERNS = _rx(
    r"^\s*what time\b",
    r"^\s*how much (is|does|do|are)\b",
    r"^\s*when (does|do|is|will)\b",
    r"^\s*where (can i|do i|is|are)\b",
    r"^\s*who (is|was|owns)\b",
    r"^\s*what does .{0,40}\bmean\b",
    r"^\s*is it (legal|normal|worth it) to\b",
)

LINK_ONLY_PATTERN = re.compile(
    r"^\s*(?:\[[^\]]*\]\(\s*)?https?://\S+\s*\)?\s*$", re.IGNORECASE
)

BOT_AUTHORS = {
    "automoderator",
    "[deleted]",
    "remindmebot",
    "sneakpeekbot",
    "totesmessenger",
    "wikitextbot",
    "b0trank",
    "botrank",
    "converter-bot",
    "haikubotinaction",
    "imagesofnetwork",
    "savevideo",
    "video_descriptionbot",
}

BOT_SUFFIX_PATTERN = re.compile(r"(?:^|[-_])bot$|bot[-_]?\d*$", re.IGNORECASE)


@dataclass
class FilterResult:
    """Why a post did or did not survive stage 1."""

    keep: bool
    reason: str
    rescued: bool = False

    def __bool__(self) -> bool:
        return self.keep


@dataclass
class FilterStats:
    total: int = 0
    kept: int = 0
    rescued: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def survival_rate(self) -> float:
        return self.kept / self.total if self.total else 0.0

    def format(self) -> str:
        lines = [
            f"posts:    {self.total}",
            f"kept:     {self.kept} ({self.survival_rate:.1%})",
            f"rescued:  {self.rescued} (soft-dropped but struggle language present)",
            "drops by reason:",
        ]
        for reason, count in sorted(self.reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<20} {count}")
        return "\n".join(lines)


def _text_of(post: Mapping[str, Any]) -> tuple[str, str]:
    title = (post.get("title") or "").strip()
    body = (post.get("body") if post.get("body") is not None else post.get("selftext")) or ""
    return title, body.strip()


def _matches(patterns: Iterable[re.Pattern[str]], *texts: str) -> bool:
    return any(p.search(t) for p in patterns for t in texts if t)


def has_struggle_language(*texts: str) -> bool:
    """First-person struggle language -- the signal that rescues a soft drop."""
    return _matches(STRUGGLE_PATTERNS, *texts)


def is_bot(author: str | None) -> bool:
    if not author:
        return True
    name = author.strip().lstrip("u/").lower()
    return name in BOT_AUTHORS or bool(BOT_SUFFIX_PATTERN.search(name))


def stage1_filter(
    post: Mapping[str, Any],
    *,
    min_body_chars: int = MIN_BODY_CHARS,
    min_score: int = MIN_SCORE,
) -> FilterResult:
    """Decide whether a post is worth spending a model call on."""
    title, body = _text_of(post)
    score = post.get("score") or 0
    num_comments = post.get("num_comments") or 0

    # Hard rules: nothing rescues these.
    if is_bot(post.get("author")):
        return FilterResult(False, "bot_author")
    if _matches(SELF_PROMO_PATTERNS, title, body):
        return FilterResult(False, "self_promo")
    if _matches(MEME_PATTERNS, title):
        return FilterResult(False, "meme")
    if body and LINK_ONLY_PATTERN.match(body):
        return FilterResult(False, "link_only")
    if len(body) < min_body_chars:
        return FilterResult(False, "too_short")

    # Soft rules: struggle language overrides them.
    struggling = has_struggle_language(title, body)

    if score < min_score and num_comments == 0:
        if not struggling:
            return FilterResult(False, "low_engagement")
        return FilterResult(True, "kept", rescued=True)

    if _matches(FACTUAL_QUESTION_PATTERNS, title):
        if not struggling:
            return FilterResult(False, "factual_question")
        return FilterResult(True, "kept", rescued=True)

    return FilterResult(True, "kept")


def summarize(posts: Iterable[Mapping[str, Any]], **kwargs) -> FilterStats:
    """Run the filter over a corpus and report the survival rate, for tuning."""
    stats = FilterStats()
    for post in posts:
        result = stage1_filter(post, **kwargs)
        stats.total += 1
        if result.keep:
            stats.kept += 1
            stats.rescued += int(result.rescued)
        else:
            stats.reasons[result.reason] = stats.reasons.get(result.reason, 0) + 1
    return stats
