"""Scenario engine: execute a list of typed steps against a zendriver Tab."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .models import (
    ClickStep,
    EvalStep,
    ExtractStep,
    FindTextStep,
    NavigateStep,
    ScreenshotStep,
    ScrollStep,
    SleepStep,
    StepResult,
    WaitForAnyStep,
    WaitForStep,
    WaitForTextStep,
)
from .settings import settings


async def _evaluate(tab, expr: str):
    """tab.evaluate bounded by a per-operation timeout. On bot-protected pages a
    single CDP evaluate can hang forever (execution context destroyed mid-reload);
    without this cap it would burn the whole run timeout. Raises TimeoutError so
    the caller fails this step fast instead of wedging the run."""
    return await asyncio.wait_for(
        tab.evaluate(expr, return_by_value=True), timeout=settings.op_timeout_s
    )


async def _eval_json(tab, js_expr: str) -> Any:
    """Evaluate a JS expression and return it as a plain Python value.

    We stringify in the page and parse here, which sidesteps CDP's
    deep-serialization quirks for arrays/objects."""
    wrapped = f"JSON.stringify((function(){{ return ({js_expr}); }})())"
    raw = await _evaluate(tab, wrapped)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def _extract_js(step: ExtractStep) -> str:
    sel = json.dumps(step.selector)
    attr = json.dumps(step.attr or "")
    read = {
        "text": "el => el.innerText",
        "html": "el => el.innerHTML",
        "attr": f"el => el.getAttribute({attr})",
    }[step.kind]
    if step.many:
        return (
            f"Array.prototype.slice.call(document.querySelectorAll({sel}))"
            f".map({read})"
        )
    return (
        f"(function(){{const el=document.querySelector({sel});"
        f"return el?({read})(el):null;}})()"
    )


async def run_steps(tab, steps, browser=None) -> tuple[dict[str, Any], list[StepResult]]:
    """Execute steps in order. Returns (data, per-step results).

    A failing step is recorded but does not abort the run, so partial data
    is still returned (useful when one selector of many is missing)."""
    data: dict[str, Any] = {}
    results: list[StepResult] = []

    for i, step in enumerate(steps):
        action = getattr(step, "action", "?")
        step_started = time.monotonic()
        try:
            if isinstance(step, NavigateStep):
                # Bound the navigation: tab.get waits for `load`, which the anti-bot
                # reload loop and the infinite-scroll SERP never fire. On timeout we
                # stop waiting and continue — the following wait_for/wait_for_any
                # decides on whatever rendered, instead of hanging to the ceiling.
                try:
                    if step.new_tab and browser is not None:
                        tab = await asyncio.wait_for(
                            browser.get(step.url, new_tab=True),
                            timeout=settings.nav_timeout_s,
                        )
                    else:
                        await asyncio.wait_for(
                            tab.get(step.url), timeout=settings.nav_timeout_s
                        )
                except asyncio.TimeoutError:
                    pass
                if step.settle_seconds:
                    await tab.sleep(step.settle_seconds)

            elif isinstance(step, WaitForStep):
                await tab.select(step.selector, timeout=step.timeout_s)

            elif isinstance(step, WaitForAnyStep):
                # Poll for whichever selector appears first; stop as soon as one
                # is present instead of blocking for the full timeout. Bound by a
                # WALL-CLOCK deadline (not an iteration count): on an unresponsive
                # page each poll's evaluate can hang up to its own timeout, so a
                # fixed iteration count would overrun massively (48 polls × 12s).
                # Use a short per-poll timeout and stop at the real deadline.
                checks = " || ".join(
                    f"!!document.querySelector({json.dumps(sel)})"
                    for sel in step.selectors
                )
                deadline = time.monotonic() + step.timeout_s
                while time.monotonic() < deadline:
                    try:
                        if await asyncio.wait_for(
                            tab.evaluate(f"({checks})", return_by_value=True),
                            timeout=min(2.0, settings.op_timeout_s),
                        ):
                            break
                    except asyncio.TimeoutError:
                        pass  # page busy/mid-reload — keep polling until deadline
                    await tab.sleep(0.25)

            elif isinstance(step, WaitForTextStep):
                await tab.find(step.text, timeout=step.timeout_s)

            elif isinstance(step, SleepStep):
                await tab.sleep(step.seconds)

            elif isinstance(step, ClickStep):
                el = await tab.select(step.selector, timeout=step.timeout_s)
                await el.click()

            elif isinstance(step, ScrollStep):
                # Bound each scroll: on a wedged page scroll_down can hang, and an
                # unbounded loop would burn the run budget after the waits already
                # decided there's nothing here.
                for _ in range(step.times):
                    try:
                        if step.direction == "down":
                            await asyncio.wait_for(tab.scroll_down(step.amount), timeout=settings.op_timeout_s)
                        else:
                            await asyncio.wait_for(tab.scroll_up(step.amount), timeout=settings.op_timeout_s)
                    except asyncio.TimeoutError:
                        break
                    await tab.sleep(0.4)

            elif isinstance(step, ExtractStep):
                data[step.name] = await _eval_json(tab, _extract_js(step))

            elif isinstance(step, FindTextStep):
                el = await tab.find(step.text, timeout=step.timeout_s)
                data[step.name] = el.text if el else None

            elif isinstance(step, EvalStep):
                value = await _eval_json(tab, step.expression)
                if step.name:
                    data[step.name] = value

            elif isinstance(step, ScreenshotStep):
                data[step.name] = await asyncio.wait_for(
                    tab.screenshot_b64(full_page=step.full_page),
                    timeout=settings.op_timeout_s,
                )

            else:  # pragma: no cover - guarded by the typed union
                raise ValueError(f"unknown step type: {type(step)!r}")

            results.append(
                StepResult(
                    index=i,
                    action=action,
                    ok=True,
                    duration_ms=int((time.monotonic() - step_started) * 1000),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report, don't abort
            results.append(
                StepResult(
                    index=i,
                    action=action,
                    ok=False,
                    error=str(exc),
                    duration_ms=int((time.monotonic() - step_started) * 1000),
                )
            )

    return data, results
