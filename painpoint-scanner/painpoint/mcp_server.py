"""MCP server: query the pain point database from a Claude conversation.

Standard stdio server. A fresh database connection is opened per call rather
than held open -- calls are infrequent, connections are cheap, and a per-call
connection sidesteps SQLite's thread affinity entirely.

Requires mcp >= 2.0, where FastMCP was renamed to MCPServer.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from . import queries
from .config import add_subreddit as config_add_subreddit
from .config import load_config, load_dotenv
from .db import Database

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError as exc:  # pragma: no cover - import-time guidance
    raise SystemExit(
        "The 'mcp' package (>= 2.0) is required for the MCP server: pip install 'mcp>=2.0'"
    ) from exc

server = MCPServer(
    name="painpoint",
    instructions=(
        "Query a database of pain points scraped from Reddit, clustered by "
        "problem and ranked by a 0-25 rubric (specificity, workflow frequency, "
        "money signal, incumbent gap, buildability). Scores are a filter, not a "
        "verdict -- always surface the thread permalinks so the user can read "
        "the primary evidence."
    ),
)


@contextmanager
def _db():
    db = Database.connect(os.environ.get("DATABASE_URL"))
    try:
        yield db
    finally:
        db.close()


def parse_since(value: str | None) -> datetime | None:
    """Accept either a relative window ('30d', '6w') or an ISO date."""
    if not value:
        return None
    text = value.strip().lower()

    relative = re.fullmatch(r"(\d+)\s*([dwm])", text)
    if relative:
        amount = int(relative.group(1))
        days = {"d": 1, "w": 7, "m": 30}[relative.group(2)] * amount
        return datetime.now(timezone.utc) - timedelta(days=days)

    parsed = datetime.fromisoformat(text.replace("z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


MEMBER_FIELDS = (
    "permalink subreddit title score num_comments pain_summary "
    "evidence_quote current_workaround competitors total_score"
).split()


def _summarize(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cluster.get("id"),
        "canonical_pain": cluster.get("canonical_pain"),
        "vertical": cluster.get("vertical"),
        "mentions": cluster.get("member_count"),
        "median_score": cluster.get("median_score"),
        "max_score": 25,
        "cluster_score": cluster.get("cluster_score"),
        "this_month": cluster.get("this_month_count"),
        "prev_month": cluster.get("prev_month_count"),
        "last_seen": str(cluster.get("last_seen") or ""),
    }


@server.tool(
    name="search_pains",
    description=(
        "Ranked pain point clusters. Filter by vertical, by minimum rubric score "
        "(0-25 median across the cluster), and by when the pain was last "
        "mentioned. Returns cluster summaries -- call get_cluster for the "
        "evidence and thread links."
    ),
)
def search_pains_tool(
    vertical: str | None = None,
    min_score: float = 0,
    since: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    with _db() as db:
        rows = queries.search_pains(
            db,
            vertical=vertical,
            min_score=min_score,
            since=parse_since(since),
            limit=max(1, min(limit, 100)),
        )
        clusters = [_summarize(r) for r in rows]
        return {"count": len(clusters), "clusters": clusters}


@server.tool(
    name="get_cluster",
    description=(
        "One cluster in full: every member post with its permalink, score, "
        "evidence quote, stated workaround, and any competitor named. The "
        "permalinks are the point -- they are what the user should go read."
    ),
)
def get_cluster_tool(cluster_id: int) -> dict[str, Any]:
    with _db() as db:
        cluster = queries.get_cluster(db, cluster_id)
        if not cluster:
            return {"error": f"No cluster with id {cluster_id}"}

        summary = _summarize(cluster)
        summary["workarounds"] = [
            {"workaround": w, "count": c} for w, c in cluster.get("workarounds", [])
        ]
        summary["competitors"] = [
            {"name": n, "count": c} for n, c in cluster.get("competitors", [])
        ]
        summary["members"] = [
            {k: m.get(k) for k in MEMBER_FIELDS}
            | {"created_utc": str(m.get("created_utc") or "")}
            for m in cluster.get("members", [])
        ]
        return summary


@server.tool(
    name="get_trending",
    description=(
        "Clusters with the steepest growth: mentions in the last N days versus "
        "the N days before. A cluster growing month over month is worth more "
        "than a bigger one that is flat."
    ),
)
def get_trending_tool(days: int = 30, limit: int = 10) -> dict[str, Any]:
    with _db() as db:
        rows = queries.get_trending(db, days=max(1, days), limit=max(1, min(limit, 50)))
        trending = []
        for row in rows:
            entry = _summarize(row)
            entry.update(
                {
                    "recent_count": row.get("recent_count"),
                    "prior_count": row.get("prior_count"),
                    "growth_ratio": row.get("growth_ratio"),
                }
            )
            trending.append(entry)
        return {"window_days": days, "count": len(trending), "clusters": trending}


@server.tool(
    name="add_subreddit",
    description=(
        "Add a subreddit to a vertical in the collector config. Takes effect on "
        "the next sweep. Creates the vertical if it does not exist."
    ),
)
def add_subreddit_tool(name: str, vertical: str) -> dict[str, Any]:
    clean = name.strip().lstrip("/").removeprefix("r/")
    if not clean:
        return {"added": False, "error": "Subreddit name is required"}

    added = config_add_subreddit(clean, vertical.strip())
    config = load_config()
    return {
        "added": added,
        "subreddit": clean,
        "vertical": vertical,
        "message": (
            f"r/{clean} added to '{vertical}'; it will be swept on the next run."
            if added
            else f"r/{clean} is already in '{vertical}'."
        ),
        "config_path": str(config.path),
    }


@server.tool(
    name="get_status",
    description="Corpus counts: posts collected, classified, and clustered.",
)
def get_status_tool() -> dict[str, Any]:
    with _db() as db:
        return queries.corpus_stats(db)


def main() -> None:
    load_dotenv()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
