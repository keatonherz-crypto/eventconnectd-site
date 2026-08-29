"""Command line entry point.

    painpoint doctor                 check credentials and connectivity
    painpoint initdb                 create tables
    painpoint collect                one sweep of Reddit into the database
    painpoint classify               score everything that passed stage 1
    painpoint classify --review 50   print recent scores for hand-checking
    painpoint filter-stats           stage 1 survival rate, for tuning
    painpoint cluster                group pain summaries, rank clusters
    painpoint report                 write the weekly digest
    painpoint status                 corpus counts
    painpoint mcp                    run the MCP server on stdio

The four stages are separate commands on purpose: each can fail without taking
the others down, and collection should keep running on its own schedule even
while you are still arguing with the classifier's output.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config, load_dotenv
from .db import Database
from .queries import corpus_stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="painpoint", description="Sweep Reddit for problems worth building against."
    )
    parser.add_argument("--config", help="Path to the YAML config file")
    parser.add_argument("--database-url", help="Overrides DATABASE_URL")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="Create tables if they do not exist")

    doctor = sub.add_parser(
        "doctor", help="Check credentials, connectivity and config before a sweep"
    )
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live API calls; only check that credentials are present",
    )

    collect = sub.add_parser("collect", help="One sweep of the configured subreddits")
    collect.add_argument("--vertical", action="append", help="Limit to a vertical (repeatable)")
    collect.add_argument("--sweep-index", type=int, help="Force the query term rotation offset")

    classify = sub.add_parser("classify", help="Run stage 2 on posts that passed stage 1")
    classify.add_argument("--limit", type=int, help="Maximum posts to classify this run")
    classify.add_argument(
        "--review",
        type=int,
        metavar="N",
        help="Print the N most recent classifications for hand-checking, then exit",
    )

    sub.add_parser("filter-stats", help="Stage 1 survival rate and drop reasons")
    sub.add_parser("cluster", help="Embed pain summaries and rebuild clusters")

    report = sub.add_parser("report", help="Write the weekly digest")
    report.add_argument("--out-dir", default="reports", help="Directory for the digest")
    report.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip narrative synthesis; counts, quotes and links only",
    )
    report.add_argument("--stdout", action="store_true", help="Print instead of writing a file")

    sub.add_parser("status", help="Corpus counts")
    sub.add_parser("mcp", help="Run the MCP server on stdio")
    return parser


def _filter_stats(db: Database) -> str:
    rows = db.query(
        "SELECT stage1_keep, stage1_reason, COUNT(*) AS n FROM posts "
        "GROUP BY stage1_keep, stage1_reason"
    )
    if not rows:
        return "No posts collected yet."

    total = sum(r["n"] for r in rows)
    kept = sum(r["n"] for r in rows if r["stage1_keep"])
    lines = [
        f"posts:  {total}",
        f"kept:   {kept} ({kept / total:.1%})",
        "",
        "drops by reason:",
    ]
    drops = sorted(
        ((r["stage1_reason"], r["n"]) for r in rows if not r["stage1_keep"]),
        key=lambda kv: -kv[1],
    )
    lines.extend(f"  {reason or 'unknown':<20} {n}" for reason, n in drops)
    lines.append("")
    lines.append(
        "Aim for roughly 10% surviving. Well above that and stage 2 is paying to "
        "read noise; well below and real complaints are being thrown away -- read "
        "a sample of the drops before tightening anything."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    if args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    config = load_config(args.config)

    if args.command == "doctor":
        from .doctor import format_report, run_checks

        checks = run_checks(config, args.database_url, offline=args.offline)
        print(format_report(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0

    with Database.connect(args.database_url) as db:
        if args.command == "initdb":
            db.init_schema()
            print(f"Schema ready ({db.dialect}).")
            return 0

        # Every other command reads tables, so make sure they exist first.
        db.init_schema()

        if args.command == "collect":
            from .collector import Collector

            stats = Collector(db, config).sweep(
                sweep_index=args.sweep_index, verticals=args.vertical
            )
            print(stats.format())
            return 1 if stats.errors and stats.posts_written == 0 else 0

        if args.command == "classify":
            from .classifier import Classifier, review_sample

            if args.review:
                print(review_sample(db, args.review))
                return 0
            stats = Classifier(db, config).run(limit=args.limit)
            print(stats.format())
            return 1 if stats.errors and stats.written == 0 else 0

        if args.command == "filter-stats":
            print(_filter_stats(db))
            return 0

        if args.command == "cluster":
            from .clusterer import Clusterer

            print(Clusterer(db, config).run().format())
            return 0

        if args.command == "report":
            from .reporter import Reporter

            reporter = Reporter(db, config)
            if args.stdout:
                print(reporter.build(use_llm=not args.no_llm))
            else:
                path = reporter.write(args.out_dir, use_llm=not args.no_llm)
                print(f"Wrote {path}")
            return 0

        if args.command == "status":
            for key, value in corpus_stats(db).items():
                print(f"{key:<15} {value}")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
