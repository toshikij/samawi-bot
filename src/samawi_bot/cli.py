"""Command line: `samawi-bot run <job> [--plan-id N] [--day YYYY-MM-DD]`, `samawi-bot admin`, `samawi-bot migrate`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from .app import App, build_app
from .config import get_settings

JOBS = ("plan", "produce", "publish", "publish-due", "metrics", "learn", "comments-dm", "tokens", "healthcheck", "evergreen", "refresh-sources")


# HTTP client loggers print full request URLs at INFO, and Meta GET requests carry the access
# token in the query string. Keep them at WARNING so no token ever reaches CloudWatch.
HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3")


def quiet_http_logs() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


STATEFUL_JOBS = frozenset({"plan", "refresh-sources"})


def restore_state(app: App) -> None:
    """Fresh containers start from the image's snapshot; restore the refreshed one if S3 has it."""
    if not app.settings.s3_bucket:
        return
    from .state import pull

    pull(app.storage, "snapshot", app.settings.snapshot_dir)
    pull(app.storage, "hadith", app.hadith_dir)


def persist_state(app: App) -> None:
    if not app.settings.s3_bucket:
        return
    from .state import push

    push(app.storage, "snapshot", app.settings.snapshot_dir)
    push(app.storage, "hadith", app.hadith_dir)


def job_fn(name: str, args: argparse.Namespace) -> Callable[[App], dict[str, Any]]:
    from .jobs import ops
    from .jobs.evergreen import top_up
    from .jobs.plan import refresh_sources, run_plan
    from .jobs.produce import run_produce
    from .jobs.publish import publish_due, publish_plan

    day = date.fromisoformat(args.day) if args.day else None
    table: dict[str, Callable[[App], dict[str, Any]]] = {
        "plan": run_plan,
        "produce": lambda app: run_produce(app, day),
        "publish": lambda app: publish_plan(app, args.plan_id) if args.plan_id else publish_due(app),
        "publish-due": publish_due,
        "metrics": ops.run_metrics,
        "learn": ops.run_learn,
        "comments-dm": ops.run_comments_dm,
        "tokens": ops.run_tokens,
        "healthcheck": ops.run_healthcheck,
        "evergreen": lambda app: top_up(app, args.count),
        "refresh-sources": refresh_sources,
    }
    return table[name]


def main(argv: Sequence[str] | None = None, app_factory: Callable[[], App] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    quiet_http_logs()
    parser = argparse.ArgumentParser(prog="samawi-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("job", choices=JOBS)
    run.add_argument("--plan-id", type=int)
    run.add_argument("--day")
    run.add_argument("--count", type=int, default=30)
    sub.add_parser("migrate")
    admin = sub.add_parser("admin")
    admin.add_argument("--port", type=int, default=3200)
    args = parser.parse_args(argv)
    app = (app_factory or (lambda: build_app(get_settings())))()
    if args.command == "migrate":
        from .db.migrate import upgrade

        upgrade(app.settings.database_url)
        return 0
    if args.command == "admin":
        import uvicorn

        from .admin.web import create_admin

        uvicorn.run(create_admin(app), host="0.0.0.0", port=args.port)  # noqa: S104 — inside a private task, reached only by tunnel
        return 0
    from .jobs.runner import run_job

    restore_state(app)
    result = run_job(app, args.job, job_fn(args.job, args))
    if result.status == "ok" and args.job in STATEFUL_JOBS:
        persist_state(app)
    # The CloudWatch dead-man alarm counts these lines (infra/terraform/alarms.tf).
    print(f"SAMAWI_HEARTBEAT job={args.job} status={result.status}")
    print(f"{args.job}: {result.status} {result.summary if result.status != 'failed' else result.error}")
    return 0 if result.status in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
