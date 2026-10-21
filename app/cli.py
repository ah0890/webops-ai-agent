"""Command-line interface: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlmodel import select


def _bootstrap():
    from app.config.settings import get_settings
    from app.database.session import init_db
    from app.logging_setup import configure_logging

    configure_logging()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    return settings


def cmd_sync(args) -> int:
    from app.config.loader import load_websites, load_workflows
    from app.database.session import session_scope
    from app.services.config_sync import sync_websites, sync_workflows

    settings = _bootstrap()
    with session_scope() as s:
        w = sync_websites(s, load_websites(settings.websites_config))
        f = sync_workflows(s, load_workflows(settings.workflows_config))
    print(json.dumps({"websites_file": str(settings.websites_config), "workflows_file": str(settings.workflows_config),
                      **w, **f}, indent=2))
    if not settings.websites_config.exists():
        print("\nNo config/websites.yaml yet — copy config/websites.example.yaml and add your real websites.")
    return 0


def cmd_import(args) -> int:
    from app.database.session import session_scope
    from app.services.importer import import_products

    settings = _bootstrap()
    path = Path(args.file)
    with session_scope() as s:
        summary = import_products(s, path.name, path.read_bytes(), settings.currency_set, dry_run=args.dry_run)
    print(json.dumps(summary.as_dict(), indent=2))
    return 0 if summary.invalid == 0 else 2


def cmd_plan(args) -> int:
    from app.agent.llm import get_provider
    from app.agent.planner import plan_workflow
    from app.database.models import Product, Website, Workflow
    from app.database.session import session_scope
    from app.scheduler.schedule import next_run_after

    settings = _bootstrap()
    with session_scope() as s:
        sites = [w.name for w in s.exec(select(Website).where(Website.active == True)).all()]  # noqa: E712
        n = len(s.exec(select(Product).where(Product.active == True)).all())  # noqa: E712
    plan = plan_workflow(args.instruction, sites, n, settings.price_change_threshold, get_provider(settings),
                         settings.llm_extraction_fallback and settings.llm_available)
    print(plan.model_dump_json(indent=2))
    if args.save:
        from app.database.models import utcnow

        with session_scope() as s:
            name = args.name or plan.name
            if s.exec(select(Workflow).where(Workflow.name == name)).first():
                print(f"workflow {name!r} exists; pass --name", file=sys.stderr)
                return 1
            wf = Workflow(name=name, description=plan.description, instruction=plan.instruction,
                          definition=plan.definition.model_dump(mode="json"),
                          plan=plan.model_dump(mode="json", exclude={"definition"}), source="planner",
                          next_run_at=next_run_after(plan.definition.schedule, utcnow()))
            s.add(wf)
            s.commit()
            s.refresh(wf)
            print(f"\nSaved workflow #{wf.id} {wf.name!r}")
    return 0


def _resolve_workflow(ref: str) -> int:
    from app.database.models import Workflow
    from app.database.session import session_scope

    with session_scope() as s:
        wf = s.get(Workflow, int(ref)) if ref.isdigit() else s.exec(select(Workflow).where(Workflow.name == ref)).first()
        if not wf:
            raise SystemExit(f"workflow {ref!r} not found (see `python -m app.cli workflows`)")
        return wf.id  # type: ignore[return-value]


def cmd_run(args) -> int:
    from app.workflows.run_service import RunService

    settings = _bootstrap()
    if args.headed:
        settings.browser_headless = False
    svc = RunService(settings)
    run = svc.create_run(_resolve_workflow(args.workflow), "cli")
    print(f"Run #{run.id} started…")
    run = svc.execute(run.id)  # type: ignore[arg-type]
    print(json.dumps({"run_id": run.id, "status": run.status, "total": run.products_total,
                      "succeeded": run.products_succeeded, "needs_review": run.products_needs_review,
                      "failed": run.products_failed, "alerts": run.alerts_count, "error": run.error,
                      "summary": run.summary}, indent=2, default=str))
    return 0 if run.status != "failed" else 1


def cmd_workflows(args) -> int:
    from app.database.models import Workflow
    from app.database.session import session_scope

    _bootstrap()
    with session_scope() as s:
        for wf in s.exec(select(Workflow)).all():
            print(f"#{wf.id:<3} {wf.name:<40} enabled={wf.enabled} schedule={wf.definition.get('schedule')} "
                  f"next_run_at={wf.next_run_at}")
    return 0


def cmd_report(args) -> int:
    from app.reporting.excel import generate_run_report

    settings = _bootstrap()
    print(generate_run_report(int(args.run_id), settings, with_ai_summary=not args.no_ai))
    return 0


def cmd_inspect(args) -> int:
    from app.browser.inspector import inspect_url

    settings = _bootstrap()
    if args.headed:
        settings.browser_headless = False
    rep = asyncio.run(inspect_url(args.url, settings))
    print(json.dumps(rep.__dict__, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    _bootstrap()
    uvicorn.run("app.api.main:app", host=args.host, port=args.port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="webops", description="WebOps AI Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="Load config/websites.yaml + config/workflows.yaml into the database").set_defaults(fn=cmd_sync)
    sp = sub.add_parser("import", help="Import products from CSV/XLSX/YAML/JSON")
    sp.add_argument("file")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_import)
    sp = sub.add_parser("plan", help="Turn a natural-language instruction into a workflow")
    sp.add_argument("instruction")
    sp.add_argument("--save", action="store_true")
    sp.add_argument("--name")
    sp.set_defaults(fn=cmd_plan)
    sp = sub.add_parser("run", help="Run a workflow now (blocking)")
    sp.add_argument("workflow", help="workflow id or name")
    sp.add_argument("--headed", action="store_true", help="show the browser window")
    sp.set_defaults(fn=cmd_run)
    sub.add_parser("workflows", help="List workflows").set_defaults(fn=cmd_workflows)
    sp = sub.add_parser("report", help="(Re)generate the Excel report for a run")
    sp.add_argument("run_id")
    sp.add_argument("--no-ai", action="store_true")
    sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("inspect", help="Inspect a real product page to discover price/availability selectors")
    sp.add_argument("url")
    sp.add_argument("--headed", action="store_true")
    sp.set_defaults(fn=cmd_inspect)
    sp = sub.add_parser("serve", help="Start API + dashboard + scheduler")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
