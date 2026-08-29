"""Shared fixtures.

The whole suite runs against a temporary SQLite file with fake Reddit and
Anthropic clients. No network, no services, no API keys.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from painpoint.classifier import INSERT_CLASSIFICATION  # noqa: E402
from painpoint.config import (  # noqa: E402
    ClassifierConfig,
    ClustererConfig,
    CollectorConfig,
    Config,
    ReporterConfig,
)
from painpoint.db import Database, ts_param, utcnow  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database.connect(f"sqlite:///{tmp_path}/test.db")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        path=tmp_path / "config.yaml",
        verticals={"events": ["eventplanning"], "trades": ["HVAC"]},
        query_terms=["is there an app", "wish there was", "manually", "workaround", "so annoying"],
        collector=CollectorConfig(
            window_hours=24, new_limit=10, search_limit=5, terms_per_sweep=2, comments_per_post=5
        ),
        classifier=ClassifierConfig(batch_size=2, max_body_chars=500),
        clusterer=ClustererConfig(embedding_backend="hashing"),
        reporter=ReporterConfig(top_n_per_vertical=2, threads_per_idea=3),
    )


def insert_post(
    db,
    post_id,
    *,
    title="A title",
    body="body text",
    vertical="events",
    subreddit="eventplanning",
    score=10,
    num_comments=4,
    days_ago=1,
    stage1_keep=1,
):
    db.execute(
        "INSERT INTO posts (id, subreddit, vertical, title, body, author, score, "
        "num_comments, created_utc, permalink, fetched_at, stage1_keep, stage1_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            post_id,
            subreddit,
            vertical,
            title,
            body,
            "someone",
            score,
            num_comments,
            ts_param(utcnow() - timedelta(days=days_ago)),
            f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/",
            ts_param(utcnow()),
            stage1_keep,
            "kept" if stage1_keep else "too_short",
        ),
    )
    return post_id


def insert_classification(
    db,
    post_id,
    *,
    summary="I lose track of which invoices I sent",
    total=18,
    is_painpoint=1,
    workaround="a spreadsheet",
    competitors=("QuickBooks",),
    quote="I lose track every month",
):
    scores = {
        "specificity": 4,
        "workflow_frequency": 4,
        "money_signal": 4,
        "incumbent_gap": 3,
        "buildability": 3,
    }
    # Deliberately the production statement, so its ON CONFLICT behaviour is
    # what the tests exercise.
    db.execute(
        INSERT_CLASSIFICATION,
        (
            post_id,
            is_painpoint,
            summary,
            "events",
            workaround,
            quote,
            db.json_param(list(competitors)),
            db.json_param(scores),
            total,
            "claude-haiku-4-5",
            ts_param(utcnow()),
        ),
    )


# -- fakes --------------------------------------------------------------


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [FakeBlock(text)]
        self.stop_reason = "end_turn"


class FakeMessages:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._responder(kwargs))


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; `responder` maps a request to reply text."""

    def __init__(self, responder):
        self.messages = FakeMessages(responder)
