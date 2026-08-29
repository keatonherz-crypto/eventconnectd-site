"""Preflight checks."""

from __future__ import annotations

import pytest

from painpoint.doctor import (
    FAIL,
    OK,
    WARN,
    check_anthropic,
    check_database,
    check_embeddings,
    check_reddit,
    format_report,
    run_checks,
)

REDDIT_VARS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")


@pytest.fixture
def no_credentials(monkeypatch):
    for var in (*REDDIT_VARS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def reddit_credentials(monkeypatch):
    for var in REDDIT_VARS:
        monkeypatch.setenv(var, "x")


class FakePost:
    id = "abc123"


class FakeReddit:
    def __init__(self, posts=(FakePost(),), error=None):
        self._posts = posts
        self._error = error

    def subreddit(self, name):
        if self._error:
            raise self._error
        return self

    def new(self, limit=None):
        return list(self._posts)[:limit]


# -- config and database ------------------------------------------------


def test_config_check_counts_targets(config):
    check = run_checks(config, offline=True)[0]
    assert check.status == OK
    assert "2 subreddits" in check.detail


def test_config_check_fails_with_no_subreddits(config):
    config.verticals = {}
    assert run_checks(config, offline=True)[0].status == FAIL


def test_database_check_creates_and_reports_schema(tmp_path):
    check = check_database(f"sqlite:///{tmp_path}/d.db")
    assert check.status == OK
    assert "0 posts" in check.detail


def test_database_check_warns_when_url_is_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    check = check_database(None)
    assert check.status == WARN
    assert "DATABASE_URL unset" in check.detail


def test_database_check_fails_on_an_unreachable_server(monkeypatch):
    """A Postgres URL that cannot be dialled must fail, not raise."""
    check = check_database("postgresql://user:pw@127.0.0.1:1/nope")
    assert check.status == FAIL


# -- reddit -------------------------------------------------------------


def test_reddit_check_names_the_missing_variables(no_credentials):
    check = check_reddit(offline=True)
    assert check.status == FAIL
    assert "REDDIT_CLIENT_ID" in check.detail


def test_reddit_check_is_unverified_when_offline(reddit_credentials):
    check = check_reddit(offline=True)
    assert check.status == WARN
    assert "not verified" in check.detail


def test_reddit_check_reads_a_post_when_online(reddit_credentials):
    check = check_reddit(offline=False, client_factory=FakeReddit)
    assert check.status == OK
    assert "abc123" in check.detail


def test_reddit_check_reports_an_auth_failure(reddit_credentials):
    def factory():
        raise RuntimeError("received 401 HTTP response")

    check = check_reddit(offline=False, client_factory=factory)
    assert check.status == FAIL
    assert "401" in check.detail


def test_reddit_check_warns_on_an_empty_listing(reddit_credentials):
    check = check_reddit(offline=False, client_factory=lambda: FakeReddit(posts=()))
    assert check.status == WARN


# -- anthropic ----------------------------------------------------------


def test_anthropic_check_requires_a_key(config, no_credentials):
    assert check_anthropic(config, offline=True).status == FAIL


def test_anthropic_check_accepts_an_auth_token(config, monkeypatch, no_credentials):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    assert check_anthropic(config, offline=True).status == WARN


def test_anthropic_check_pings_the_configured_model(config, monkeypatch):
    from tests.conftest import FakeAnthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client = FakeAnthropic(lambda request: "ok")

    check = check_anthropic(config, offline=False, client_factory=lambda: client)

    assert check.status == OK
    assert config.classifier.model in check.detail
    # A ping must stay a ping -- one token, not a real classification.
    assert client.messages.calls[0]["max_tokens"] == 1


def test_anthropic_check_reports_a_bad_key(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    def factory():
        raise RuntimeError("invalid x-api-key")

    check = check_anthropic(config, offline=False, client_factory=factory)
    assert check.status == FAIL
    assert "invalid x-api-key" in check.detail


# -- embeddings and reporting -------------------------------------------


def test_embeddings_check_needs_no_key_for_the_hashing_backend(config, no_credentials):
    check = check_embeddings(config)
    assert check.status == OK
    assert "hashing" in check.detail


def test_embeddings_check_fails_when_openai_is_selected_without_a_key(config, no_credentials):
    config.clusterer.embedding_backend = "openai"
    assert check_embeddings(config).status == FAIL


def test_report_summarizes_failures(config, no_credentials, tmp_path):
    checks = run_checks(config, f"sqlite:///{tmp_path}/d.db", offline=True)
    report = format_report(checks)

    assert "[FAIL] reddit" in report
    assert "2 check(s) failed" in report
    assert "never in the config file" in report


def test_report_confirms_a_clean_run(config, monkeypatch, reddit_credentials, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    checks = run_checks(config, f"sqlite:///{tmp_path}/d.db", offline=True)

    assert format_report(checks).endswith("All checks passed. Ready to collect.")
