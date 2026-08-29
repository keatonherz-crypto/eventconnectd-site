"""Preflight checks: is this thing actually wired up?

Answers "are my credentials working" before a sweep burns an hour finding out.
Each check reports ok / warn / fail independently, so one missing key does not
hide the state of everything else.

Network checks are the point -- a key that is merely *present* tells you
nothing -- but `--offline` skips them when you only want to verify config and
schema. The Reddit check reads a single post and the Anthropic check asks for a
single token, so a full run costs a fraction of a cent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .db import Database

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _env_present(*names: str) -> list[str]:
    return [n for n in names if not os.environ.get(n)]


def check_config(config: Config) -> Check:
    subs = config.all_subs()
    if not subs:
        return Check("config", FAIL, f"No subreddits configured in {config.path}")
    return Check(
        "config",
        OK,
        f"{len(subs)} subreddits across {len(config.verticals)} verticals "
        f"({config.path})",
    )


def check_database(db_url: str | None) -> Check:
    url = db_url or os.environ.get("DATABASE_URL")
    if not url:
        return Check(
            "database", WARN, "DATABASE_URL unset; defaulting to sqlite:///painpoints.db"
        )
    try:
        with Database.connect(url) as db:
            db.init_schema()
            counts = db.scalar("SELECT COUNT(*) FROM posts")
        scheme = url.split(":", 1)[0]
        return Check("database", OK, f"{scheme} reachable, schema present, {counts} posts")
    except Exception as exc:
        return Check("database", FAIL, f"Cannot open {url.split('://')[0]} database: {exc}")


def check_reddit(offline: bool, client_factory: Callable[[], Any] | None = None) -> Check:
    missing = _env_present("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
    if missing:
        return Check("reddit", FAIL, f"Missing: {', '.join(missing)}")
    if offline:
        return Check("reddit", WARN, "Credentials present; not verified (--offline)")

    try:
        from .collector import build_reddit_client

        reddit = (client_factory or build_reddit_client)()
        # One post from one sub is enough to prove the OAuth handshake works.
        post = next(iter(reddit.subreddit("smallbusiness").new(limit=1)), None)
        if post is None:
            return Check("reddit", WARN, "Authenticated, but the listing came back empty")
        return Check("reddit", OK, f"Authenticated; read r/smallbusiness ({post.id})")
    except Exception as exc:
        return Check("reddit", FAIL, f"{type(exc).__name__}: {exc}")


def check_anthropic(
    config: Config, offline: bool, client_factory: Callable[[], Any] | None = None
) -> Check:
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return Check("anthropic", FAIL, "Missing: ANTHROPIC_API_KEY")
    if offline:
        return Check("anthropic", WARN, "Credentials present; not verified (--offline)")

    model = config.classifier.model
    try:
        if client_factory:
            client = client_factory()
        else:
            import anthropic

            client = anthropic.Anthropic()
        client.messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
        )
        return Check("anthropic", OK, f"Authenticated; {model} responded")
    except Exception as exc:
        return Check("anthropic", FAIL, f"{type(exc).__name__} on {model}: {exc}")


def check_embeddings(config: Config) -> Check:
    backend = config.clusterer.embedding_backend
    if backend != "openai":
        return Check("embeddings", OK, f"Using the '{backend}' backend; no API key needed")
    if not os.environ.get("OPENAI_API_KEY"):
        return Check("embeddings", FAIL, "Backend is 'openai' but OPENAI_API_KEY is unset")
    return Check("embeddings", OK, "OPENAI_API_KEY present")


def run_checks(config: Config, db_url: str | None = None, offline: bool = False) -> list[Check]:
    return [
        check_config(config),
        check_database(db_url),
        check_reddit(offline),
        check_anthropic(config, offline),
        check_embeddings(config),
    ]


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"[{MARKS[c.status]}] {c.name:<{width}}  {c.detail}" for c in checks]

    failed = [c for c in checks if c.status == FAIL]
    lines.append("")
    if failed:
        lines.append(
            f"{len(failed)} check(s) failed. Fix these before running a sweep: "
            + ", ".join(c.name for c in failed)
        )
        lines.append("Credentials belong in .env (see .env.example), never in the config file.")
    else:
        lines.append("All checks passed. Ready to collect.")
    return "\n".join(lines)
