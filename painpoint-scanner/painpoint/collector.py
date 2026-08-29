"""Stage 0: pull posts and comments off Reddit into the database.

Notes on what this can and cannot do:

* Listing endpoints cap out around 1000 items, and Pushshift has been
  moderator-only since 2023, so there is no historical backfill to be had. This
  is a forward-looking collector: the dataset gets more valuable the longer it
  runs, and the sweep window deliberately overlaps the cron interval because
  duplicates are free (dedup on Reddit ID) and gaps are not.
* PRAW handles OAuth token refresh and the ~100 requests/minute limit itself.
  Don't add manual throttling on top; it just makes sweeps slower without
  making them safer.
* A subreddit that is private, banned, or renamed raises at iteration time. One
  bad sub must not kill the sweep, so failures are recorded per-sub and the
  loop continues.

The Reddit client is injected rather than constructed inline, which is what
lets the tests drive the whole sweep against a fake.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from .config import Config
from .db import Database, ts_param, utcnow
from .heuristics import stage1_filter

log = logging.getLogger(__name__)

UPSERT_POST = """
INSERT INTO posts (
  id, subreddit, vertical, title, body, author, score, num_comments,
  created_utc, permalink, fetched_at, stage1_keep, stage1_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
  score = excluded.score,
  num_comments = excluded.num_comments,
  fetched_at = excluded.fetched_at,
  stage1_keep = excluded.stage1_keep,
  stage1_reason = excluded.stage1_reason
"""

INSERT_COMMENT = """
INSERT INTO comments (id, post_id, body, author, score, created_utc, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO NOTHING
"""


@dataclass
class SweepStats:
    subs_swept: int = 0
    posts_seen: int = 0
    posts_written: int = 0
    posts_kept: int = 0
    comments_written: int = 0
    errors: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"subs swept:       {self.subs_swept}",
            f"posts seen:       {self.posts_seen}",
            f"posts written:    {self.posts_written}",
            f"passed stage 1:   {self.posts_kept}",
            f"comments written: {self.comments_written}",
        ]
        if self.errors:
            lines.append(f"errors ({len(self.errors)}):")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)


def build_reddit_client():
    """Construct a read-only PRAW client from the environment."""
    import praw

    missing = [
        name
        for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "Missing Reddit credentials: "
            + ", ".join(missing)
            + ". Create a 'script' app at reddit.com/prefs/apps and set them in .env."
        )

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )
    reddit.read_only = True
    return reddit


def _author_name(obj: Any) -> str | None:
    author = getattr(obj, "author", None)
    if author is None:
        return None
    return getattr(author, "name", None) or str(author)


def _epoch_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _permalink(submission: Any) -> str:
    link = getattr(submission, "permalink", "") or ""
    return f"https://www.reddit.com{link}" if link.startswith("/") else link


class Collector:
    def __init__(self, db: Database, config: Config, reddit: Any | None = None):
        self.db = db
        self.config = config
        self._reddit = reddit

    @property
    def reddit(self) -> Any:
        if self._reddit is None:
            self._reddit = build_reddit_client()
        return self._reddit

    # -- term rotation --------------------------------------------------

    def terms_for_sweep(self, sweep_index: int) -> list[str]:
        """A slice of the query terms, rotated so a day of sweeps covers them all."""
        terms = self.config.query_terms
        n = self.config.collector.terms_per_sweep
        if not terms or n <= 0:
            return []
        if n >= len(terms):
            return list(terms)
        start = (sweep_index * n) % len(terms)
        doubled = terms + terms
        return doubled[start : start + n]

    # -- sweep ----------------------------------------------------------

    def sweep(
        self,
        *,
        sweep_index: int | None = None,
        verticals: Sequence[str] | None = None,
    ) -> SweepStats:
        cfg = self.config.collector
        if sweep_index is None:
            # Stateless rotation: the hour number is enough to advance the
            # window between cron runs without persisting a counter.
            sweep_index = int(utcnow().timestamp() // 3600)

        terms = self.terms_for_sweep(sweep_index)
        cutoff = utcnow() - timedelta(hours=cfg.window_hours)
        stats = SweepStats()
        seen: set[str] = set()

        targets = [
            (vertical, sub)
            for vertical, sub in self.config.all_subs()
            if verticals is None or vertical in verticals
        ]

        for vertical, sub in targets:
            stats.subs_swept += 1
            try:
                subreddit = self.reddit.subreddit(sub)
                submissions = list(self._listings(subreddit, terms, cfg))
            except Exception as exc:  # one dead sub must not end the sweep
                log.warning("r/%s failed: %s", sub, exc)
                stats.errors.append(f"r/{sub}: {exc}")
                continue

            for submission in submissions:
                post_id = getattr(submission, "fullname", None) or f"t3_{submission.id}"
                if post_id in seen:
                    continue
                seen.add(post_id)
                stats.posts_seen += 1

                created = _epoch_to_dt(getattr(submission, "created_utc", None))
                if created and created < cutoff:
                    continue

                try:
                    kept = self._store_post(post_id, submission, sub, vertical, created)
                except Exception as exc:
                    log.warning("failed to store %s: %s", post_id, exc)
                    stats.errors.append(f"{post_id}: {exc}")
                    continue

                stats.posts_written += 1
                if not kept:
                    continue

                stats.posts_kept += 1
                # Comments are where the detail lives, so they are only worth
                # fetching for posts that already look like a real complaint.
                try:
                    stats.comments_written += self._store_comments(post_id, submission)
                except Exception as exc:
                    log.warning("comments for %s failed: %s", post_id, exc)
                    stats.errors.append(f"comments {post_id}: {exc}")

            self.db.commit()

        return stats

    def _listings(self, subreddit: Any, terms: Sequence[str], cfg) -> Iterator[Any]:
        yield from subreddit.new(limit=cfg.new_limit)
        for term in terms:
            yield from subreddit.search(
                f'"{term}"', sort="new", time_filter="month", limit=cfg.search_limit
            )

    def _store_post(
        self,
        post_id: str,
        submission: Any,
        sub: str,
        vertical: str,
        created: datetime | None,
    ) -> bool:
        record = {
            "title": getattr(submission, "title", "") or "",
            "body": getattr(submission, "selftext", "") or "",
            "author": _author_name(submission),
            "score": getattr(submission, "score", 0) or 0,
            "num_comments": getattr(submission, "num_comments", 0) or 0,
        }
        verdict = stage1_filter(record)

        self.db.execute(
            UPSERT_POST,
            (
                post_id,
                sub,
                vertical,
                record["title"],
                record["body"],
                record["author"],
                record["score"],
                record["num_comments"],
                ts_param(created),
                _permalink(submission),
                ts_param(utcnow()),
                1 if verdict.keep else 0,
                verdict.reason,
            ),
        )
        return verdict.keep

    def _store_comments(self, post_id: str, submission: Any) -> int:
        comments = getattr(submission, "comments", None)
        if comments is None:
            return 0
        # Collapse "load more comments" stubs rather than paging into them --
        # top-level replies carry the detail we are after.
        replace_more = getattr(comments, "replace_more", None)
        if callable(replace_more):
            replace_more(limit=0)

        limit = self.config.collector.comments_per_post
        now = ts_param(utcnow())
        rows = []
        for comment in list(comments)[:limit]:
            comment_id = getattr(comment, "fullname", None) or f"t1_{comment.id}"
            rows.append(
                (
                    comment_id,
                    post_id,
                    getattr(comment, "body", "") or "",
                    _author_name(comment),
                    getattr(comment, "score", 0) or 0,
                    ts_param(_epoch_to_dt(getattr(comment, "created_utc", None))),
                    now,
                )
            )
        return self.db.executemany(INSERT_COMMENT, rows)
