"""Configuration loading.

Everything tunable lives in config/subreddits.yaml. Secrets live in the
environment (.env), never in the config file and never in code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "subreddits.yaml"


@dataclass
class CollectorConfig:
    window_hours: int = 8
    new_limit: int = 100
    search_limit: int = 50
    terms_per_sweep: int = 4
    comments_per_post: int = 25


@dataclass
class ClassifierConfig:
    model: str = "claude-haiku-4-5"
    batch_size: int = 20
    max_body_chars: int = 1500


@dataclass
class ClustererConfig:
    # None means "use the backend's own default" -- a threshold tuned for
    # semantic embeddings is meaningless applied to lexical ones.
    similarity_threshold: float | None = None
    embedding_backend: str = "hashing"
    openai_model: str = "text-embedding-3-small"


@dataclass
class ReporterConfig:
    top_n_per_vertical: int = 5
    threads_per_idea: int = 5
    synthesis_model: str = "claude-opus-5"


@dataclass
class Config:
    path: Path
    verticals: dict[str, list[str]] = field(default_factory=dict)
    query_terms: list[str] = field(default_factory=list)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    clusterer: ClustererConfig = field(default_factory=ClustererConfig)
    reporter: ReporterConfig = field(default_factory=ReporterConfig)

    def vertical_for(self, subreddit: str) -> str:
        """Reverse lookup: which vertical does this sub belong to?"""
        target = subreddit.lower()
        for vertical, subs in self.verticals.items():
            if any(sub.lower() == target for sub in subs):
                return vertical
        return "unknown"

    def all_subs(self) -> list[tuple[str, str]]:
        """(vertical, subreddit) pairs, in config order."""
        return [(v, sub) for v, subs in self.verticals.items() for sub in subs]


def _section(raw: dict[str, Any], key: str, cls):
    values = raw.get(key) or {}
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in values.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    import yaml  # imported lazily so the pure-Python modules stay dependency-free

    resolved = Path(path or os.environ.get("PAINPOINT_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(resolved.read_text()) or {}

    verticals = {
        name: list(body.get("subs") or [])
        for name, body in (raw.get("verticals") or {}).items()
    }

    return Config(
        path=resolved,
        verticals=verticals,
        query_terms=list(raw.get("query_terms") or []),
        collector=_section(raw, "collector", CollectorConfig),
        classifier=_section(raw, "classifier", ClassifierConfig),
        clusterer=_section(raw, "clusterer", ClustererConfig),
        reporter=_section(raw, "reporter", ReporterConfig),
    )


def add_subreddit(name: str, vertical: str, path: str | Path | None = None) -> bool:
    """Add a sub to a vertical in the config file. Returns False if already present.

    Rewrites the YAML through the parser, so comments in the file are lost. That
    is the tradeoff for letting the MCP server edit config at runtime.
    """
    import yaml

    resolved = Path(path or os.environ.get("PAINPOINT_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(resolved.read_text()) or {}
    verticals = raw.setdefault("verticals", {})
    entry = verticals.setdefault(vertical, {"subs": []})
    subs = entry.setdefault("subs", [])

    if any(s.lower() == name.lower() for s in subs):
        return False

    subs.append(name)
    resolved.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False))
    return True


def load_dotenv(path: str | Path | None = None) -> None:
    """Minimal .env loader. Existing environment variables always win."""
    resolved = Path(path or Path.cwd() / ".env")
    if not resolved.exists():
        return
    for line in resolved.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
