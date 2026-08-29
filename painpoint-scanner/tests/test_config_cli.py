"""Config loading, the CLI surface, and the MCP server's helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import yaml

from painpoint import cli
from painpoint.config import DEFAULT_CONFIG_PATH, add_subreddit, load_config, load_dotenv
from tests.conftest import insert_classification, insert_post

SAMPLE = {
    "verticals": {
        "events": {"subs": ["eventplanning", "weddingplanning"]},
        "trades": {"subs": ["HVAC"]},
    },
    "query_terms": ["wish there was"],
    "collector": {"window_hours": 3, "new_limit": 7},
    "classifier": {"model": "claude-haiku-4-5", "batch_size": 4},
    "clusterer": {"embedding_backend": "hashing"},
    "reporter": {"top_n_per_vertical": 3},
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "subreddits.yaml"
    path.write_text(yaml.safe_dump(SAMPLE))
    return path


# -- config -------------------------------------------------------------


def test_shipped_config_is_valid():
    """The checked-in config must parse -- it is what a first run uses."""
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.verticals
    assert config.query_terms
    assert config.classifier.model.startswith("claude-")


def test_load_config_reads_every_section(config_file):
    config = load_config(config_file)

    assert config.verticals["events"] == ["eventplanning", "weddingplanning"]
    assert config.collector.window_hours == 3
    assert config.collector.new_limit == 7
    assert config.classifier.batch_size == 4
    assert config.reporter.top_n_per_vertical == 3
    # Unset keys keep their dataclass defaults rather than becoming None.
    assert config.collector.search_limit == 50


def test_unknown_config_keys_are_ignored(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"collector": {"window_hours": 2, "nonsense": 1}}))

    assert load_config(path).collector.window_hours == 2


def test_vertical_lookup_is_case_insensitive(config_file):
    config = load_config(config_file)

    assert config.vertical_for("hvac") == "trades"
    assert config.vertical_for("EventPlanning") == "events"
    assert config.vertical_for("neverheardofit") == "unknown"


def test_all_subs_pairs_each_sub_with_its_vertical(config_file):
    assert ("trades", "HVAC") in load_config(config_file).all_subs()


def test_add_subreddit_appends_and_is_idempotent(config_file):
    assert add_subreddit("sweatystartup", "smallbusiness", config_file) is True
    assert add_subreddit("sweatystartup", "smallbusiness", config_file) is False

    config = load_config(config_file)
    assert config.verticals["smallbusiness"] == ["sweatystartup"]
    assert config.verticals["events"] == ["eventplanning", "weddingplanning"]


def test_dotenv_never_overrides_the_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('EXISTING=from_file\nFRESH="from_file"\n# comment\ngarbage\n')
    monkeypatch.setitem(os.environ, "EXISTING", "from_shell")
    monkeypatch.delitem(os.environ, "FRESH", raising=False)

    load_dotenv(env)

    assert os.environ["EXISTING"] == "from_shell"
    assert os.environ["FRESH"] == "from_file"


def test_dotenv_missing_file_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "nope.env")


# -- MCP helpers --------------------------------------------------------


def test_parse_since_accepts_relative_windows():
    from painpoint.mcp_server import parse_since

    now = datetime.now(timezone.utc)

    def days_back(window):
        return (now - parse_since(window)).total_seconds() / 86400

    assert 29.9 < days_back("30d") < 30.1
    assert 41.9 < days_back("6w") < 42.1
    assert 29.9 < days_back("1m") < 30.1
    assert parse_since(None) is None


def test_parse_since_accepts_iso_dates():
    from painpoint.mcp_server import parse_since

    assert parse_since("2026-03-01").year == 2026
    assert parse_since("2026-03-01").tzinfo is not None


def test_mcp_exposes_the_documented_tools():
    """The four tools named in the spec, under those exact names."""
    import asyncio

    from painpoint.mcp_server import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"search_pains", "get_cluster", "get_trending", "add_subreddit"} <= names


# -- CLI ----------------------------------------------------------------


def run_cli(args, tmp_path, config_file, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path}/cli.db"
    code = cli.main(["--config", str(config_file), "--database-url", url, *args])
    return code, capsys.readouterr().out


def test_initdb_creates_the_schema(tmp_path, config_file, monkeypatch, capsys):
    code, out = run_cli(["initdb"], tmp_path, config_file, monkeypatch, capsys)
    assert code == 0
    assert "Schema ready" in out


def test_status_reports_counts(tmp_path, config_file, monkeypatch, capsys):
    code, out = run_cli(["status"], tmp_path, config_file, monkeypatch, capsys)
    assert code == 0
    assert "posts" in out and "clusters" in out


def test_filter_stats_reports_survival(tmp_path, config_file, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    from painpoint.db import Database

    with Database.connect(url) as db:
        db.init_schema()
        insert_post(db, "t3_keep", stage1_keep=1)
        insert_post(db, "t3_drop", stage1_keep=0)

    code, out = run_cli(["filter-stats"], tmp_path, config_file, monkeypatch, capsys)

    assert code == 0
    assert "50.0%" in out
    assert "too_short" in out


def test_filter_stats_on_an_empty_database(tmp_path, config_file, monkeypatch, capsys):
    _, out = run_cli(["filter-stats"], tmp_path, config_file, monkeypatch, capsys)
    assert "No posts collected yet" in out


def test_report_writes_a_file(tmp_path, config_file, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path}/cli.db"
    from painpoint.clusterer import Clusterer
    from painpoint.db import Database

    with Database.connect(url) as db:
        db.init_schema()
        insert_post(db, "t3_a")
        insert_classification(db, "t3_a")
        Clusterer(db, load_config(config_file)).run()

    code, out = run_cli(
        ["report", "--no-llm", "--out-dir", str(tmp_path / "out")],
        tmp_path,
        config_file,
        monkeypatch,
        capsys,
    )

    assert code == 0
    assert "Wrote" in out
    assert list((tmp_path / "out").glob("digest-*.md"))


def test_unknown_command_exits_nonzero(tmp_path, config_file, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(["frobnicate"], tmp_path, config_file, monkeypatch, capsys)
    assert exc.value.code != 0


def test_mcp_tools_execute_end_to_end(db, config, monkeypatch, tmp_path):
    """Call the tools through the server, the way a client would."""
    import asyncio
    import json

    from painpoint.clusterer import Clusterer
    from painpoint.mcp_server import server

    insert_post(db, "t3_a", title="Invoice trouble")
    insert_classification(db, "t3_a")
    Clusterer(db, config).run()
    db.commit()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        return json.loads(result.content[0].text)

    listed = call("search_pains", {"limit": 5})
    assert listed["count"] == 1
    cluster_id = listed["clusters"][0]["id"]

    detail = call("get_cluster", {"cluster_id": cluster_id})
    assert detail["members"][0]["permalink"].startswith("https://")
    assert detail["competitors"] == [{"name": "QuickBooks", "count": 1}]

    assert call("get_trending", {"days": 30})["clusters"][0]["id"] == cluster_id
    assert call("get_status", {})["clusters"] == 1
    assert call("get_cluster", {"cluster_id": 9999})["error"]


def test_mcp_add_subreddit_writes_the_config(config_file, monkeypatch):
    import asyncio
    import json

    from painpoint.mcp_server import server

    monkeypatch.setenv("PAINPOINT_CONFIG", str(config_file))

    result = asyncio.run(
        server.call_tool("add_subreddit", {"name": "r/sweatystartup", "vertical": "trades"})
    )
    payload = json.loads(result.content[0].text)

    assert payload["added"] is True
    assert payload["subreddit"] == "sweatystartup"
    assert "sweatystartup" in load_config(config_file).verticals["trades"]
