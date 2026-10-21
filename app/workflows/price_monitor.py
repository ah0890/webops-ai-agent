"""The price-monitoring workflow: real browser → extraction → validation → storage → comparison → alerts.

Failure isolation: a product failing never stops its website; a website failing never stops the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.agent.llm import LLMProvider
from app.agent.recovery import ai_strategy, deterministic_strategies
from app.browser.executor import BrowserExecutor
from app.browser.extractor import DataExtractor
from app.browser.manager import BrowserManager, slug
from app.browser.observer import PageObserver, PageState
from app.config.schema import AvailabilityRules, LoginConfig, SelectorSet, WorkflowDefinition
from app.config.settings import Settings
from app.database.models import (
    Alert,
    ExtractionStatus,
    Observation,
    Product,
    RunStatus,
    Website,
    Workflow,
    WorkflowRun,
    utcnow,
)
from app.database.session import session_scope
from app.models.actions import GotoAction, WaitAction, parse_action, parse_actions
from app.models.extraction import ExtractionResult, Issue, ValidationOutcome
from app.security.robots import RobotsPolicy
from app.security.url_guard import UrlGuard
from app.services import alerts as alert_engine
from app.services.intelligence import pct_change, previous_trusted
from app.services.validation import PreviousObservation, ProductContext, ValidationPolicy, validate_extraction
from app.workflows.audit import AuditRecorder

log = logging.getLogger(__name__)
_RETRYABLE = {"missing_price", "no_data", "extraction_error", "navigation_failed", "http_retryable"}


@dataclass
class SiteRuntime:
    website: Website
    guard: UrlGuard
    selectors: SelectorSet | None
    rules: AvailabilityRules
    pre_actions: list
    login: LoginConfig | None
    delay: float
    respect_robots: bool


def select_targets(session: Session, defn: WorkflowDefinition, limit: int) -> list[tuple[Website, Product]]:
    q = (select(Website, Product).join(Product, Product.website_id == Website.id)
         .where(Website.active == True, Product.active == True))  # noqa: E712
    if defn.websites and defn.websites != ["*"]:
        q = q.where(col(Website.name).in_(defn.websites))
    if defn.skus:
        q = q.where(col(Product.sku).in_(defn.skus))
    return list(session.exec(q.order_by(Website.id, Product.id).limit(limit)).all())


class PriceMonitorRunner:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None, robots: RobotsPolicy | None = None):
        self.settings = settings
        self.provider = provider
        self.robots = robots or RobotsPolicy(user_agent=settings.user_agent_suffix or "*")
        self.observer = PageObserver()
        self.extractor = DataExtractor(provider, settings.llm_extraction_fallback)
        self._new_repairs: dict[int, list[str]] = defaultdict(list)
        self.policy = ValidationPolicy(supported_currencies=settings.currency_set,
                                       max_plausible_change_pct=settings.max_plausible_change_pct)

    # ------------------------------------------------------------------ run
    async def run(self, run_id: int) -> WorkflowRun:
        audit = AuditRecorder(run_id)
        started = time.perf_counter()
        with session_scope() as s:
            run = s.get(WorkflowRun, run_id)
            wf = s.get(Workflow, run.workflow_id)  # type: ignore[union-attr]
            defn = WorkflowDefinition.model_validate(wf.definition)  # type: ignore[union-attr]
            targets = select_targets(s, defn, self.settings.max_products_per_run)
            run.status, run.started_at, run.products_total = RunStatus.RUNNING.value, utcnow(), len(targets)  # type: ignore[union-attr]
            s.add(run)
            s.commit()
            wf_name = wf.name  # type: ignore[union-attr]
        audit.log(f"Run started for workflow '{wf_name}': {len(targets)} product(s)", websites=defn.websites)

        if not targets:
            return self._finish(run_id, audit, defn, started,
                                error="No active products match this workflow. Add real products to "
                                      "config/websites.yaml or import a CSV, then sync.")
        by_site: dict[int, list[Product]] = defaultdict(list)
        sites: dict[int, Website] = {}
        for w, p in targets:
            sites[w.id] = w  # type: ignore[index]
            by_site[w.id].append(p)  # type: ignore[index]

        error = None
        sem = asyncio.Semaphore(self.settings.max_concurrency)
        try:
            async with BrowserManager(self.settings) as bm:
                audit.step("browser", message=f"Chromium {bm.browser.version} launched "  # type: ignore[union-attr]
                                              f"(headless={self.settings.browser_headless})")
                await asyncio.wait_for(
                    asyncio.gather(*(self._site(bm, sem, run_id, audit, sites[sid], prods, defn)
                                     for sid, prods in by_site.items())),
                    timeout=self.settings.run_timeout_seconds)
        except TimeoutError:
            error = f"run exceeded RUN_TIMEOUT_SECONDS={self.settings.run_timeout_seconds}; remaining products skipped"
            audit.log(error, "ERROR")
        except Exception as e:  # browser failed to launch etc.
            log.exception("run %s crashed", run_id)
            error = f"run error: {type(e).__name__}: {e}"
            audit.log(error, "ERROR")
        return self._finish(run_id, audit, defn, started, error=error)

    # ----------------------------------------------------------------- site
    def _site_runtime(self, w: Website) -> SiteRuntime:
        cfg = w.config or {}
        robots_cfg = cfg.get("respect_robots_txt")
        return SiteRuntime(
            website=w, guard=UrlGuard(w.allowed_domains),
            selectors=SelectorSet.model_validate(cfg["selectors"]) if cfg.get("selectors") else None,
            rules=AvailabilityRules.model_validate(cfg.get("availability_rules") or {}),
            pre_actions=parse_actions(cfg.get("pre_extract_actions") or []),
            login=LoginConfig.model_validate(cfg["login"]) if cfg.get("login") else None,
            delay=float(cfg.get("request_delay_seconds", self.settings.default_request_delay_seconds)),
            respect_robots=self.settings.respect_robots_txt if robots_cfg is None else bool(robots_cfg),
        )

    async def _site(self, bm: BrowserManager, sem: asyncio.Semaphore, run_id: int, audit: AuditRecorder,
                    website: Website, products: list[Product], defn: WorkflowDefinition) -> None:
        async with sem:
            try:
                rt = self._site_runtime(website)
            except (ValidationError, ValueError) as e:
                audit.step("open_website", status="failed", website_id=website.id,
                           message=f"invalid website configuration: {e}")
                for p in products:
                    self._store_failure(run_id, audit, website, p, "config_error", f"invalid website config: {e}", None)
                return
            # A saved session exists only if the user created one (login config or `cli login-session`).
            ctx = await bm.new_context(website.name, rt.guard, use_saved_session=not (rt.login and not rt.login.reuse_session))
            ex = BrowserExecutor(ctx, rt.guard, self.settings.screenshots_dir, f"run_{run_id}/{slug(website.name)}",
                                 self.settings.downloads_dir / f"run_{run_id}")
            try:
                if rt.login and not await self._login(bm, ctx, ex, audit, rt):
                    for p in products:
                        self._store_failure(run_id, audit, website, p, "login_failed",
                                            "authorized login did not succeed; see login step", None)
                    return
                for i, product in enumerate(products):
                    if i:
                        await asyncio.sleep(max(rt.delay, self.robots.crawl_delay(product.url) or 0))
                    try:
                        await self._product(run_id, audit, ex, rt, product, defn)
                    except Exception as e:  # last-resort isolation
                        log.exception("unexpected failure on product %s", product.id)
                        self._store_failure(run_id, audit, website, product, "unexpected_error",
                                            f"{type(e).__name__}: {e}", None)
            finally:
                await ctx.close()

    async def _login(self, bm: BrowserManager, ctx, ex: BrowserExecutor, audit: AuditRecorder, rt: SiteRuntime) -> bool:
        login = rt.login
        assert login
        wid = rt.website.id
        res = await ex.execute(GotoAction(url=login.url))
        if login.success_selector and res.ok:
            page = await ex.ensure_page()
            if await page.locator(login.success_selector).count() > 0:
                audit.step("login", website_id=wid, message="reused saved authorized session")
                return True
        for step in login.steps:
            r = await ex.execute(step)
            audit.step("login", action=r.action, detail=r.detail, website_id=wid, duration_ms=r.duration_ms,
                       status="ok" if r.ok else "failed", message=r.error)
            if not r.ok:
                shot = await self._safe_shot(ex, "login_error")
                if shot:
                    audit.screenshot(shot, "error", wid, None)
                return False
        if login.success_selector:
            ok = (await ex.execute(WaitAction(selector=login.success_selector, optional=False))).ok
            audit.step("login", website_id=wid, status="ok" if ok else "failed",
                       message="logged in" if ok else f"success selector {login.success_selector!r} not found")
            if not ok:
                return False
        if login.reuse_session:
            await bm.save_session(ctx, rt.website.name)
        return True

    # -------------------------------------------------------------- product
    async def _product(self, run_id: int, audit: AuditRecorder, ex: BrowserExecutor, rt: SiteRuntime,
                       product: Product, defn: WorkflowDefinition) -> None:
        wid, pid = rt.website.id, product.id
        step = lambda stage, **kw: audit.step(stage, website_id=wid, product_id=pid, **kw)  # noqa: E731

        if rt.respect_robots:
            allowed = await asyncio.to_thread(self.robots.allowed, product.url)
            if not allowed:
                step("open_website", status="failed", message="robots.txt disallows this URL; skipped")
                self._store_failure(run_id, audit, rt.website, product, "robots_disallowed",
                                    "robots.txt disallows this URL (set respect_robots_txt: false only if you "
                                    "are authorized)", None)
                return

        attempts = 1 + self.settings.max_retries
        strategies = deterministic_strategies()
        extraction: ExtractionResult | None = None
        outcome: ValidationOutcome | None = None
        state: PageState | None = None
        shot_before: str | None = None
        expected_ccy = product.currency or rt.website.currency
        selectors = SelectorSet.model_validate(product.selectors).merged_over(rt.selectors) if product.selectors \
            else rt.selectors
        previous = previous_trusted_obs(run_id, pid)  # type: ignore[arg-type]

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await self._recover(ex, audit, rt, product, attempt, strategies, state, outcome)
            res = await ex.execute(GotoAction(url=product.url, wait_until="domcontentloaded"))
            status_code = res.detail.get("status")
            step("open_website", action="goto", detail=res.detail, attempt=attempt, duration_ms=res.duration_ms,
                 status="ok" if res.ok else "failed",
                 message=res.error or f"HTTP {status_code} — {res.detail.get('final_url')}")
            if not res.ok:
                outcome = _failed("navigation_failed", res.error or "navigation failed")
                if "allowlist" in (res.error or ""):
                    break
                if attempt < attempts:  # the final attempt's screenshot is taken after the loop
                    await self._error_shot(ex, audit, wid, pid)
                continue

            for act in rt.pre_actions:
                r = await ex.execute(act)
                step("pre_extract", action=r.action, detail=r.detail, attempt=attempt, duration_ms=r.duration_ms,
                     status="ok" if r.ok else "warning", message=r.error)
            if selectors and selectors.price and "@" not in selectors.price and (status_code or 200) < 400:
                await ex.execute(WaitAction(selector=selectors.price, optional=True))
            await ex.execute(WaitAction(load_state="load", optional=True))

            page = await ex.ensure_page()
            state = await self.observer.observe(page, status_code)
            step("locate_product", attempt=attempt, detail=state.summary(),
                 status="failed" if state.blocked or state.not_found else "ok",
                 message=_state_message(state))
            if state.captcha:
                outcome = _failed("blocked_challenge", "bot challenge / CAPTCHA shown — not bypassed by design")
                break
            if state.login_wall:
                outcome = _failed("login_required", "page requires login; configure an authorized login for this site")
                break
            if state.not_found:
                outcome = _failed("not_found", f"product page not found (HTTP {status_code}) — check the URL")
                break
            if status_code and (status_code == 429 or status_code >= 500):
                outcome = _failed("http_retryable", f"HTTP {status_code}")
                if attempt < attempts:
                    await self._error_shot(ex, audit, wid, pid)
                    await asyncio.sleep(min(30, 5 * attempt))
                continue

            shot_before = await self._safe_shot(ex, f"p{pid}_before_extraction")
            if shot_before:
                audit.screenshot(shot_before, "before_extraction", wid, pid)
            extraction = await self.extractor.extract(
                page, requested_url=product.url, http_status=status_code, selectors=selectors, rules=rt.rules,
                expected_currency=expected_ccy, product_name=product.name)
            ch = extraction.chosen
            step("extract", attempt=attempt, screenshot_path=shot_before,
                 status="ok" if ch and ch.price is not None else ("warning" if ch else "failed"),
                 detail=extraction.raw_dump(),
                 message=(f"{ch.source}: price={ch.price_text!r} currency={ch.currency} availability={ch.availability}"
                          if ch else extraction.error or "no product data found"))
            outcome = validate_extraction(
                extraction,
                ProductContext(name=product.name, url=product.url, sku=product.sku, currency=expected_ccy,
                               expected_name_contains=product.expected_name_contains),
                previous, self.policy)
            step("validate", attempt=attempt, status=_validation_step_status(outcome.status),
                 detail={"status": outcome.status, "issues": [i.model_dump() for i in outcome.issues]},
                 message=f"{outcome.status}" + (f": {'; '.join(i.code for i in outcome.issues)}" if outcome.issues else ""))
            if outcome.status == ExtractionStatus.FAILED.value and attempt < attempts and \
                    {i.code for i in outcome.issues if i.severity == "fatal"} <= _RETRYABLE:
                await self._error_shot(ex, audit, wid, pid)
                continue
            break

        assert outcome is not None
        if self.settings.selector_healing and extraction is not None and selectors and                 not (state and (state.blocked or state.not_found)):
            await self._heal_selectors(run_id, audit, ex, rt, product, extraction, selectors, previous, expected_ccy)
        final_shot = None
        if outcome.status != ExtractionStatus.FAILED.value:
            final_shot = await self._safe_shot(ex, f"p{pid}_after_extraction")
            if final_shot:
                audit.screenshot(final_shot, "after_extraction", wid, pid)
        else:
            final_shot = await self._error_shot(ex, audit, wid, pid)
        obs = self._store(run_id, rt.website, product, extraction, outcome, final_shot or shot_before)
        step("store", status="ok", message=f"observation #{obs.id} stored as {obs.extraction_status}")
        self._compare_and_alert(run_id, audit, rt.website, product, outcome, previous, defn, obs)

    async def _heal_selectors(self, run_id, audit, ex, rt, product, extraction, selectors, previous, expected_ccy) -> None:
        """Propose (never apply) replacements for configured selectors that no longer work."""
        from app.browser.selector_healer import propose
        from app.services.selector_repair import record

        try:
            page = await ex.ensure_page()
            proposals = await propose(page, extraction, selectors, previous.price if previous else None, expected_ccy)
        except Exception as e:
            log.info("selector healing skipped: %s", e)
            return
        for prop in proposals:
            with session_scope() as s:
                sug, is_new = record(s, website=rt.website, product=product, run_id=run_id, proposal=prop)
            if is_new:
                self._new_repairs[rt.website.id].append(f"{prop.field}: {prop.old_selector!r} -> {prop.selector!r}")
            audit.step("selector_repair", website_id=rt.website.id, product_id=product.id, status="warning",
                       message=f"configured {prop.field} selector {prop.old_selector!r} looks broken; proposed "
                               f"{prop.selector!r} (confidence {prop.confidence:.2f}, anchor {prop.anchor_source}="
                               f"{prop.anchor_value}) - pending approval #{sug.id}",
                       detail={"suggestion_id": sug.id, "alternatives": prop.alternatives, "sample": prop.sample_text})

    async def _recover(self, ex, audit, rt, product, attempt, strategies, state, outcome) -> None:
        idx = attempt - 2
        name, raw_actions = strategies[min(idx, len(strategies) - 1)]
        actions = [parse_action(a) for a in raw_actions]
        if idx == len(strategies) - 1 and self.provider and outcome is not None and state is not None:
            try:
                page = await ex.ensure_page()
                text = await page.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''")
                err = "; ".join(i.message for i in outcome.issues)
                ai_name, ai_actions = await asyncio.to_thread(ai_strategy, self.provider, state.summary(), text, err)
                if ai_actions:
                    name, actions = ai_name, ai_actions
            except Exception as e:
                log.info("AI recovery skipped: %s", e)
        audit.step("recovery", website_id=rt.website.id, product_id=product.id, attempt=attempt,
                   message=f"strategy {name} ({len(actions)} action(s)) before retry",
                   detail={"actions": [a.model_dump(exclude_none=True) for a in actions]})
        # Strategy actions run after the next goto would discard them, so run them now on the current page,
        # then the retry re-navigates (which is itself the "reload").
        for a in actions:
            await ex.execute(a)

    # ------------------------------------------------------------- storage
    def _store(self, run_id: int, website: Website, product: Product, extraction: ExtractionResult | None,
               outcome: ValidationOutcome, shot: str | None) -> Observation:
        with session_scope() as s:
            existing = s.exec(select(Observation).where(Observation.run_id == run_id,
                                                        Observation.product_id == product.id)).first()
            if existing:  # duplicate within a run — keep the first, never overwrite
                return existing
            obs = Observation(
                run_id=run_id, product_id=product.id, website_id=website.id, url=product.url,  # type: ignore[arg-type]
                final_url=extraction.final_url if extraction else None,
                page_title=extraction.page_title if extraction else None,
                http_status=extraction.http_status if extraction else None,
                price=round(outcome.price, 4) if outcome.price is not None else None, price_text=outcome.price_text,
                currency=outcome.currency, availability=outcome.availability,
                product_name_seen=outcome.product_name, sku_seen=outcome.sku,
                extraction_method=outcome.method, extraction_status=outcome.status,
                issues=[i.model_dump() for i in outcome.issues],
                raw=extraction.raw_dump() if extraction else {}, screenshot_path=shot,
            )
            s.add(obs)
            s.commit()
            s.refresh(obs)
            return obs

    def _store_failure(self, run_id, audit, website, product, code, message, shot) -> None:
        outcome = _failed(code, message)
        obs = self._store(run_id, website, product, None, outcome, shot)
        audit.step("store", website_id=website.id, product_id=product.id, status="failed",
                   message=f"observation #{obs.id} stored as FAILED ({code})")
        self._persist_alerts(run_id, website, product,
                             [alert_engine.AlertDraft("extraction_failure", "medium", f"{product.name}: {message}")], audit)

    def _compare_and_alert(self, run_id, audit, website, product, outcome, previous, defn, obs) -> None:
        wid, pid = website.id, product.id
        trusted = outcome.status in (ExtractionStatus.VERIFIED.value, ExtractionStatus.LIKELY_VALID.value)
        if not trusted:
            audit.step("compare", website_id=wid, product_id=pid, status="skipped",
                       message=f"not compared: observation is {outcome.status} (excluded from trusted history)")
        elif previous:
            pct = pct_change(outcome.price, previous.price)
            audit.step("compare", website_id=wid, product_id=pid,
                       message=f"previous {previous.price} {previous.currency or ''} → current {outcome.price} "
                               f"{outcome.currency or ''}" + (f" ({pct:+.2f}%)" if pct is not None else ""),
                       detail={"previous_price": previous.price, "current_price": outcome.price, "pct_change": pct})
        elif previous is None:
            audit.step("compare", website_id=wid, product_id=pid, status="skipped",
                       message="first trusted observation — baseline recorded")
        threshold = product.price_change_threshold or defn.price_change_threshold or self.settings.price_change_threshold
        rules = alert_engine.AlertRules(threshold, defn.detect_increases, defn.detect_decreases, defn.detect_availability)
        drafts = alert_engine.evaluate(outcome, previous, rules, product.name)
        self._persist_alerts(run_id, website, product, drafts, audit)

    def _persist_alerts(self, run_id, website, product, drafts, audit) -> None:
        if not drafts:
            audit.step("alert", website_id=website.id, product_id=product.id, status="skipped",
                       message="no alert conditions met")
            return
        webhook = self.settings.alert_webhook_url.get_secret_value() if self.settings.alert_webhook_url else None
        with session_scope() as s:
            for d in drafts:
                a = Alert(run_id=run_id, website_id=website.id, product_id=product.id, alert_type=d.alert_type,
                          severity=d.severity, reason=d.reason, previous_value=d.previous_value,
                          current_value=d.current_value, percentage_change=d.percentage_change)
                s.add(a)
                s.commit()
                audit.step("alert", website_id=website.id, product_id=product.id, status="ok",
                           message=f"{d.severity.upper()} {d.alert_type}: {d.reason}")
                alert_engine.notify(a, webhook, {"website": website.name, "product": product.name, "url": product.url})

    # ------------------------------------------------------------- helpers
    async def _safe_shot(self, ex: BrowserExecutor, name: str) -> str | None:
        try:
            return await ex.screenshot(name)
        except Exception as e:
            log.info("screenshot %s failed: %s", name, e)
            return None

    async def _error_shot(self, ex, audit, wid, pid) -> str | None:
        shot = await self._safe_shot(ex, f"p{pid}_error" if pid else "error")
        if shot:
            audit.screenshot(shot, "error", wid, pid)
        return shot

    def _finish(self, run_id: int, audit: AuditRecorder, defn: WorkflowDefinition, started: float,
                error: str | None) -> WorkflowRun:
        with session_scope() as s:
            run = s.get(WorkflowRun, run_id)
            obs = s.exec(select(Observation).where(Observation.run_id == run_id)).all()
            alerts = s.exec(select(Alert).where(Alert.run_id == run_id)).all()
            by_status: dict[str, int] = defaultdict(int)
            for o in obs:
                by_status[o.extraction_status] += 1
            # Website-level failure: every product on a site failed.
            per_site: dict[int, list[Observation]] = defaultdict(list)
            for o in obs:
                per_site[o.website_id].append(o)
            for wid, items in per_site.items():
                if items and all(o.extraction_status == ExtractionStatus.FAILED.value for o in items):
                    site = s.get(Website, wid)
                    a = Alert(run_id=run_id, website_id=wid, alert_type="website_failure", severity="high",
                              reason=f"All {len(items)} product check(s) on {site.name if site else wid} failed")
                    s.add(a)
                    alerts.append(a)
            for wid, repairs in self._new_repairs.items():
                site = s.get(Website, wid)
                a = Alert(run_id=run_id, website_id=wid, alert_type="selector_broken", severity="medium",
                          reason=f"Page structure changed on {site.name if site else wid}: {len(repairs)} selector "
                                 f"repair(s) proposed and awaiting approval ({'; '.join(repairs[:3])})")
                s.add(a)
                alerts.append(a)
            run.products_succeeded = by_status[ExtractionStatus.VERIFIED.value] + by_status[ExtractionStatus.LIKELY_VALID.value]  # type: ignore[union-attr]
            run.products_needs_review = by_status[ExtractionStatus.NEEDS_REVIEW.value]  # type: ignore[union-attr]
            run.products_failed = by_status[ExtractionStatus.FAILED.value]  # type: ignore[union-attr]
            run.alerts_count = len(alerts)  # type: ignore[union-attr]
            run.error = error  # type: ignore[union-attr]
            run.finished_at = utcnow()  # type: ignore[union-attr]
            if error and not obs:
                run.status = RunStatus.FAILED.value  # type: ignore[union-attr]
            elif error or run.products_failed or len(obs) < run.products_total:  # type: ignore[union-attr]
                run.status = RunStatus.COMPLETED_WITH_ERRORS.value  # type: ignore[union-attr]
            else:
                run.status = RunStatus.COMPLETED.value  # type: ignore[union-attr]
            types: dict[str, int] = defaultdict(int)
            for a in alerts:
                types[a.alert_type] += 1
            run.summary = {  # type: ignore[union-attr]
                "duration_seconds": round(time.perf_counter() - started, 1),
                "observations": len(obs), "by_status": dict(by_status), "alerts_by_type": dict(types),
                "websites": len(per_site),
            }
            s.add(run)
            s.commit()
            s.refresh(run)
        audit.log(f"Run finished: {run.status} — {run.products_succeeded} ok, {run.products_needs_review} review, "
                  f"{run.products_failed} failed, {run.alerts_count} alert(s)")
        if defn.generate_excel and run.status != RunStatus.FAILED.value:
            try:
                from app.reporting.excel import generate_run_report

                path = generate_run_report(run_id, self.settings)
                audit.log(f"Excel report generated: {path.name}")
            except Exception as e:
                log.exception("report generation failed")
                audit.log(f"Excel report failed: {e}", "ERROR")
        return run


def previous_trusted_obs(run_id: int, product_id: int) -> PreviousObservation | None:
    with session_scope() as s:
        o = previous_trusted(s, product_id, exclude_run_id=run_id)
        return PreviousObservation(o.price, o.currency, o.availability) if o else None


def _failed(code: str, message: str) -> ValidationOutcome:
    return ValidationOutcome(status=ExtractionStatus.FAILED.value,
                             issues=[Issue(code=code, severity="fatal", message=message)])


def _validation_step_status(status: str) -> str:
    return {"VERIFIED": "ok", "LIKELY_VALID": "ok", "NEEDS_REVIEW": "warning"}.get(status, "failed")


def _state_message(st: PageState) -> str:
    if st.captcha:
        return "bot challenge / CAPTCHA detected"
    if st.login_wall:
        return "login wall detected"
    if st.not_found:
        return f"page looks like 'not found' (HTTP {st.http_status})"
    return f"page loaded: {st.title[:120]!r}"
