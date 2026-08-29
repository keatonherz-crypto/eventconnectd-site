"""Stage 3: group repeated complaints into countable problems.

Ranking happens at the cluster level, never the post level. One person having a
bad day is noise; forty people describing the same workflow over a month is a
signal, and the only way to see the difference is to count.

The algorithm is a single greedy pass: sort pain summaries by score, and assign
each to the first existing cluster whose centroid it is close enough to, or
start a new one. This is not the best clustering algorithm available, but it is
deterministic, has no dependencies, runs in seconds on the volumes involved,
and produces stable cluster membership across weekly recomputes -- which
matters more here than marginal cluster quality, because the whole point is
tracking whether a cluster is growing.

Clustering is scoped per vertical. Pain summaries from r/HVAC and r/nursing
that happen to share vocabulary are not the same problem.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from .config import Config
from .db import Database, parse_ts, ts_param, utcnow
from .embeddings import cosine, get_embedder

log = logging.getLogger(__name__)

# Full weight for anything mentioned in the last 90 days, then a 45-day half
# life. A pain that stopped being mentioned either got solved or was never real.
RECENCY_FULL_DAYS = 90
RECENCY_HALF_LIFE_DAYS = 45

PAINPOINTS_SQL = """
SELECT c.post_id, c.pain_summary, c.total_score, p.vertical, p.created_utc
FROM classifications c JOIN posts p ON p.id = c.post_id
WHERE c.is_painpoint = 1 AND c.pain_summary IS NOT NULL AND c.pain_summary <> ''
ORDER BY c.total_score DESC, p.created_utc DESC
"""

CACHED_EMBEDDINGS_SQL = "SELECT post_id, vector FROM embeddings WHERE backend = ?"

UPSERT_EMBEDDING = """
INSERT INTO embeddings (post_id, backend, dim, vector, embedded_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (post_id) DO UPDATE SET
  backend = excluded.backend,
  dim = excluded.dim,
  vector = excluded.vector,
  embedded_at = excluded.embedded_at
"""

INSERT_CLUSTER = """
INSERT INTO clusters (
  canonical_pain, vertical, member_count, first_seen, last_seen, avg_score,
  median_score, cluster_score, prev_month_count, this_month_count, computed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_MEMBER = """
INSERT INTO cluster_members (cluster_id, post_id, similarity) VALUES (?, ?, ?)
"""


def recency_weight(last_seen: datetime | None, now: datetime | None = None) -> float:
    if last_seen is None:
        return 0.0
    now = now or utcnow()
    days = (now - last_seen).total_seconds() / 86400
    if days <= RECENCY_FULL_DAYS:
        return 1.0
    return 0.5 ** ((days - RECENCY_FULL_DAYS) / RECENCY_HALF_LIFE_DAYS)


def cluster_score(
    scores: Sequence[float], member_count: int, weight: float
) -> float:
    """median(total_score) * log10(members + 1) * recency_weight."""
    if not scores or member_count <= 0:
        return 0.0
    return statistics.median(scores) * math.log10(member_count + 1) * weight


@dataclass
class _Cluster:
    vertical: str
    centroid: list[float]
    members: list[tuple[str, float]] = field(default_factory=list)  # (post_id, similarity)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, row: dict[str, Any], vector: Sequence[float], similarity: float) -> None:
        n = len(self.rows)
        self.centroid = [(c * n + v) / (n + 1) for c, v in zip(self.centroid, vector)]
        self.members.append((row["post_id"], similarity))
        self.rows.append(row)


@dataclass
class ClusterStats:
    painpoints: int = 0
    embedded: int = 0
    cached: int = 0
    clusters: int = 0
    multi_member: int = 0

    def format(self) -> str:
        return "\n".join(
            [
                f"pain points:        {self.painpoints}",
                f"embeddings reused:  {self.cached}",
                f"embeddings created: {self.embedded}",
                f"clusters:           {self.clusters}",
                f"with 2+ members:    {self.multi_member}",
            ]
        )


class Clusterer:
    def __init__(self, db: Database, config: Config, embedder: Any | None = None):
        self.db = db
        self.config = config
        self.embedder = embedder or get_embedder(
            config.clusterer.embedding_backend, config.clusterer.openai_model
        )
        configured = config.clusterer.similarity_threshold
        self.threshold = (
            float(configured) if configured is not None else self.embedder.default_threshold
        )

    # -- embeddings -----------------------------------------------------

    def _vectors_for(self, rows: Sequence[dict[str, Any]], stats: ClusterStats):
        cached = {
            r["post_id"]: Database.load_json(r["vector"], [])
            for r in self.db.query(CACHED_EMBEDDINGS_SQL, (self.embedder.name,))
        }
        missing = [r for r in rows if not cached.get(r["post_id"])]
        stats.cached = len(rows) - len(missing)

        if missing:
            vectors = self.embedder.embed([r["pain_summary"] for r in missing])
            now = ts_param(utcnow())
            self.db.executemany(
                UPSERT_EMBEDDING,
                [
                    (
                        row["post_id"],
                        self.embedder.name,
                        len(vector),
                        self.db.json_param(vector),
                        now,
                    )
                    for row, vector in zip(missing, vectors)
                ],
            )
            self.db.commit()
            stats.embedded = len(missing)
            cached.update(
                {row["post_id"]: vector for row, vector in zip(missing, vectors)}
            )

        return cached

    # -- clustering -----------------------------------------------------

    def _assign(self, rows: Sequence[dict[str, Any]], vectors) -> list[_Cluster]:
        clusters: list[_Cluster] = []
        for row in rows:
            vector = vectors.get(row["post_id"])
            if not vector:
                continue
            vertical = row.get("vertical") or "unknown"

            best: _Cluster | None = None
            best_sim = 0.0
            for cluster in clusters:
                if cluster.vertical != vertical:
                    continue
                sim = cosine(cluster.centroid, vector)
                if sim > best_sim:
                    best, best_sim = cluster, sim

            if best is not None and best_sim >= self.threshold:
                best.add(row, vector, best_sim)
            else:
                new = _Cluster(vertical=vertical, centroid=list(vector))
                new.add(row, vector, 1.0)
                clusters.append(new)
        return clusters

    def run(self) -> ClusterStats:
        stats = ClusterStats()
        rows = self.db.query(PAINPOINTS_SQL)
        stats.painpoints = len(rows)
        if not rows:
            return stats

        vectors = self._vectors_for(rows, stats)
        clusters = self._assign(rows, vectors)

        # Clustering is recomputed from scratch each time rather than updated
        # incrementally: membership shifts as the corpus grows, and a stale
        # partial assignment would quietly distort the counts.
        self.db.execute("DELETE FROM cluster_members")
        self.db.execute("DELETE FROM clusters")

        now = utcnow()
        for cluster in clusters:
            self._persist(cluster, now)
            stats.clusters += 1
            stats.multi_member += int(len(cluster.rows) > 1)

        self.db.commit()
        return stats

    def _persist(self, cluster: _Cluster, now: datetime) -> None:
        dates = [d for d in (parse_ts(r.get("created_utc")) for r in cluster.rows) if d]
        scores = [float(r.get("total_score") or 0) for r in cluster.rows]
        member_count = len(cluster.rows)
        last_seen = max(dates) if dates else None

        month_ago, two_months_ago = now - timedelta(days=30), now - timedelta(days=60)
        this_month = sum(1 for d in dates if d >= month_ago)
        prev_month = sum(1 for d in dates if two_months_ago <= d < month_ago)

        inserted = self.db.query_one(
            INSERT_CLUSTER + " RETURNING id",
            (
                # The highest-scoring member's summary is the cluster label:
                # rows arrive score-sorted, so that is the first one.
                cluster.rows[0]["pain_summary"],
                cluster.vertical,
                member_count,
                ts_param(min(dates) if dates else None),
                ts_param(last_seen),
                round(statistics.mean(scores), 2) if scores else 0.0,
                round(statistics.median(scores), 2) if scores else 0.0,
                round(cluster_score(scores, member_count, recency_weight(last_seen, now)), 4),
                prev_month,
                this_month,
                ts_param(now),
            ),
        )
        cluster_id = inserted["id"]
        self.db.executemany(
            INSERT_MEMBER,
            [(cluster_id, post_id, round(sim, 4)) for post_id, sim in cluster.members],
        )
