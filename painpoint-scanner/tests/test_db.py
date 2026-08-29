"""Storage layer: round-tripping, JSON columns, timestamps, dedup."""

from datetime import datetime, timezone

import pytest

from painpoint.db import Database, parse_ts, ts_param, utcnow
from tests.conftest import insert_classification, insert_post


def test_schema_is_idempotent(db):
    db.init_schema()
    tables = {
        r["name"]
        for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"posts", "comments", "classifications", "clusters", "cluster_members"} <= tables


def test_post_round_trip(db):
    insert_post(db, "t3_abc", title="Title here")
    row = db.query_one("SELECT * FROM posts WHERE id = ?", ("t3_abc",))
    assert row["title"] == "Title here"
    assert row["vertical"] == "events"
    assert parse_ts(row["created_utc"]) < utcnow()


def test_json_columns_round_trip(db):
    insert_post(db, "t3_abc")
    insert_classification(db, "t3_abc", competitors=("QuickBooks", "Jobber"))
    row = db.query_one("SELECT scores, competitors FROM classifications")
    assert Database.load_json(row["competitors"]) == ["QuickBooks", "Jobber"]
    assert Database.load_json(row["scores"])["money_signal"] == 4


def test_load_json_tolerates_garbage():
    assert Database.load_json(None, []) == []
    assert Database.load_json("not json", {}) == {}
    assert Database.load_json({"already": "decoded"}) == {"already": "decoded"}


def test_classification_is_never_overwritten(db):
    """Re-running the classifier must not re-score, or costs compound silently."""
    insert_post(db, "t3_abc")
    insert_classification(db, "t3_abc", summary="first verdict", total=18)
    insert_classification(db, "t3_abc", summary="second verdict", total=3)

    rows = db.query("SELECT pain_summary, total_score FROM classifications")
    assert len(rows) == 1
    assert rows[0]["pain_summary"] == "first verdict"
    assert rows[0]["total_score"] == 18


def test_timestamp_helpers_are_inverse():
    moment = datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc)
    assert parse_ts(ts_param(moment)) == moment


def test_parse_ts_assumes_utc_for_naive_values():
    assert parse_ts("2026-03-01T12:00:00").tzinfo == timezone.utc
    assert parse_ts(datetime(2026, 3, 1)).tzinfo == timezone.utc
    assert parse_ts(None) is None
    assert parse_ts("not a date") is None


def test_placeholders_are_rewritten_for_postgres():
    postgres = Database(connection=None, dialect="postgres")
    assert postgres._render("SELECT * FROM posts WHERE id = ?") == (
        "SELECT * FROM posts WHERE id = %s"
    )


def test_rollback_on_error(tmp_path):
    with pytest.raises(RuntimeError):
        with Database.connect(f"sqlite:///{tmp_path}/x.db") as db:
            db.init_schema()
            insert_post(db, "t3_gone")
            raise RuntimeError("boom")

    with Database.connect(f"sqlite:///{tmp_path}/x.db") as db:
        assert db.scalar("SELECT COUNT(*) FROM posts") == 0


def test_executemany_ignores_empty_batches(db):
    assert db.executemany("INSERT INTO posts (id) VALUES (?)", []) == 0
