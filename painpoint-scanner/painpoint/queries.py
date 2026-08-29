"""Read queries shared by the reporter and the MCP server.

Both surfaces answer the same questions -- what are the top clusters, what is in
one, what is growing -- so the SQL lives here once rather than drifting apart in
two places.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .db import Database, parse_ts, ts_param, utcnow

CLUSTER_COLUMNS = """
  id, canonical_pain, vertical, member_count, first_seen, last_seen,
  avg_score, median_score, cluster_score, prev_month_count, this_month_count
"""

MEMBERS_SQL = """
SELECT p.id, p.title, p.permalink, p.subreddit, p.score, p.num_comments,
       p.created_utc, c.pain_summary, c.current_workaround, c.evidence_quote,
       c.competitors, c.total_score, m.similarity
FROM cluster_members m
JOIN posts p ON p.id = m.post_id
JOIN classifications c ON c.post_id = m.post_id
WHERE m.cluster_id = ?
ORDER BY c.total_score DESC, p.score DESC
"""

GROWTH_SQL = """
SELECT m.cluster_id, p.created_utc
FROM cluster_members m JOIN posts p ON p.id = m.post_id
"""


def search_pains(
    db: Database,
    vertical: str | None = None,
    min_score: float = 0.0,
    since: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Ranked clusters. `min_score` filters on the 0-25 rubric median."""
    sql = f"SELECT {CLUSTER_COLUMNS} FROM clusters WHERE median_score >= ?"
    params: list[Any] = [min_score]

    if vertical:
        sql += " AND vertical = ?"
        params.append(vertical)
    if since:
        sql += " AND last_seen >= ?"
        params.append(ts_param(since))

    sql += " ORDER BY cluster_score DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def get_cluster(db: Database, cluster_id: int) -> dict[str, Any] | None:
    """A cluster plus every member post, its permalink, and its evidence."""
    cluster = db.query_one(
        f"SELECT {CLUSTER_COLUMNS} FROM clusters WHERE id = ?", (cluster_id,)
    )
    if not cluster:
        return None

    members = db.query(MEMBERS_SQL, (cluster_id,))
    for member in members:
        member["competitors"] = Database.load_json(member.get("competitors"), []) or []

    cluster["members"] = members
    cluster["workarounds"] = tally(m.get("current_workaround") for m in members)
    cluster["competitors"] = tally(
        c for m in members for c in (m.get("competitors") or [])
    )
    return cluster


def get_trending(db: Database, days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
    """Clusters with the steepest growth: mentions in the last `days` vs the `days` before.

    A cluster that is growing month over month is worth more than a bigger one
    that is flat, so the sort is on the ratio, with the absolute count only
    breaking ties.
    """
    now = utcnow()
    recent_cutoff = now - timedelta(days=days)
    prior_cutoff = now - timedelta(days=days * 2)

    recent: Counter[int] = Counter()
    prior: Counter[int] = Counter()
    for row in db.query(GROWTH_SQL):
        created = parse_ts(row.get("created_utc"))
        if created is None:
            continue
        if created >= recent_cutoff:
            recent[row["cluster_id"]] += 1
        elif created >= prior_cutoff:
            prior[row["cluster_id"]] += 1

    clusters = db.query(f"SELECT {CLUSTER_COLUMNS} FROM clusters")
    out = []
    for cluster in clusters:
        r, p = recent.get(cluster["id"], 0), prior.get(cluster["id"], 0)
        if r == 0:
            continue
        cluster["recent_count"] = r
        cluster["prior_count"] = p
        # No prior mentions means brand new rather than infinitely trending;
        # treat it as a doubling so it ranks well without swamping everything.
        cluster["growth_ratio"] = round(r / p, 2) if p else 2.0
        out.append(cluster)

    out.sort(key=lambda c: (c["growth_ratio"], c["recent_count"]), reverse=True)
    return out[:limit]


def top_by_vertical(
    db: Database, per_vertical: int = 5, min_members: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """The top N clusters in each vertical, for the weekly digest."""
    rows = db.query(
        f"SELECT {CLUSTER_COLUMNS} FROM clusters WHERE member_count >= ? "
        "ORDER BY vertical, cluster_score DESC",
        (min_members,),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = grouped.setdefault(row["vertical"], [])
        if len(bucket) < per_vertical:
            bucket.append(row)
    return grouped


def tally(values) -> list[tuple[str, int]]:
    """Count non-empty strings, most common first."""
    counter: Counter[str] = Counter()
    for value in values:
        text = (value or "").strip()
        if text and text.lower() not in {"null", "none", "n/a"}:
            counter[text] += 1
    return counter.most_common()


def corpus_stats(db: Database) -> dict[str, Any]:
    """Headline numbers for the digest and the `status` command."""
    scalar = db.scalar
    return {
        "posts": scalar("SELECT COUNT(*) FROM posts") or 0,
        "passed_stage1": scalar("SELECT COUNT(*) FROM posts WHERE stage1_keep = 1") or 0,
        "comments": scalar("SELECT COUNT(*) FROM comments") or 0,
        "classified": scalar("SELECT COUNT(*) FROM classifications") or 0,
        "painpoints": scalar(
            "SELECT COUNT(*) FROM classifications WHERE is_painpoint = 1"
        )
        or 0,
        "clusters": scalar("SELECT COUNT(*) FROM clusters") or 0,
    }
