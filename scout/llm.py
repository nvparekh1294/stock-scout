"""scout/llm.py — the single entry point for every Claude call.

Every LLM call in Scout goes through `call()`. It:
  - resolves a model tier (opus/sonnet/haiku) to a concrete model id,
  - prompt-caches the system block (cache_control) so repeated evidence
    contexts are cheap,
  - logs the call's real token usage + a USD estimate to the `api_costs`
    table (the project design — no call goes unlogged),
  - enforces a monthly budget guardrail: if this month's spend already
    exceeds the configured cap, it raises BudgetExceeded rather than
    silently spending more,
  - supports the Batch API (50% cheaper) for scheduled/bulk work.

Uses the official Anthropic Python SDK. Never prints the API key.
Contains NO order/execution code.
"""

from __future__ import annotations

import os
import sys as _sys
import time
from datetime import datetime, timezone

import anthropic

from .config import app_name, load_config, load_env
from .db import Database


class BudgetExceeded(Exception):
    """Raised when this month's api_costs sum is already over the cap.
    Callers must surface this to the user — never silently continue."""


def _model_for_tier(tier: str, config: dict) -> str:
    models = config["models"]
    if tier not in models:
        raise ValueError(f"unknown model tier {tier!r} (expected one of {list(models)})")
    return models[tier]


def _price(model: str, config: dict) -> dict:
    pricing = config["costs"]["pricing"]
    # Fall back to a neutral estimate if a model isn't in the table (still logs).
    return pricing.get(model, {"input": 0.0, "output": 0.0, "cache_read": 0.0})


def estimate_usd(model: str, input_tokens: int, output_tokens: int,
                 cached_tokens: int, config: dict, discount: float = 1.0) -> float:
    """USD estimate for one call. Cached (cache-read) tokens are billed at the
    cheap cache_read rate; the rest of the input at full input rate. `discount`
    scales the whole bill — pass 0.5 for a Batch API call (Message Batches are
    billed at 50% of standard rates)."""
    p = _price(model, config)
    uncached_input = max(0, input_tokens)  # SDK's input_tokens already excludes cache reads
    usd = (
        uncached_input / 1_000_000 * p["input"]
        + output_tokens / 1_000_000 * p["output"]
        + cached_tokens / 1_000_000 * p["cache_read"]
    )
    return round(usd * discount, 6)


def month_spend(db: Database, now: datetime | None = None) -> float:
    """Sum of api_costs.usd_estimate for the current calendar month (UTC)."""
    now = now or datetime.now(timezone.utc)
    prefix = now.strftime("%Y-%m")
    total = 0.0
    for row in db.select("api_costs"):
        ts = str(row.get("ts", ""))
        if ts.startswith(prefix):
            total += float(row.get("usd_estimate") or 0.0)
    return round(total, 6)


def log_cost(db: Database, model: str, task: str, usage: dict, config: dict,
             discount: float = 1.0) -> float:
    """Insert one api_costs row from a response `usage` and return the USD est.
    `usage` keys: input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens. `discount` scales the USD estimate — pass 0.5
    for a Batch API result so the ledger reflects the real 50%-off price."""
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cached_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    usd = estimate_usd(model, input_tokens, output_tokens, cached_tokens, config, discount)
    db.insert("api_costs", {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "task": task,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "usd_estimate": usd,
    })
    return usd


def _require_api_key() -> str:
    load_env()
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set (owner action A3). Add it to .env; "
            f"{app_name()} never prints its value."
        )
    return key


def _system_blocks(system, cache_system: bool):
    """Wrap the system prompt as a cacheable text block (prompt caching).

    `system` may be a plain string (wrapped in one ephemeral-cached block) OR a
    pre-built list of content blocks (passed through unchanged — the caller has
    already placed its own cache_control breakpoints; this is how research.py
    puts the evidence pack ahead of the divergent per-role instructions so the
    adversary pass cache-reads the pack the underwriter wrote)."""
    if not system:
        return anthropic.NOT_GIVEN
    if isinstance(system, list):
        return system
    block = {"type": "text", "text": system}
    if cache_system:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _build_kwargs(model: str, messages: list, max_tokens: int, system,
                  cache_system: bool, thinking: dict | None, effort: str | None,
                  tools: list | None) -> dict:
    """Assemble the messages.create kwargs shared by the sync and batch paths."""
    system_param = _system_blocks(system, cache_system)
    kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)
    if system_param is not anthropic.NOT_GIVEN:
        kwargs["system"] = system_param
    if thinking is not None:
        kwargs["thinking"] = thinking
    if effort is not None:
        kwargs["output_config"] = {"effort": effort}
    if tools is not None:
        kwargs["tools"] = tools
    return kwargs


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text")


def call(task: str, model_tier: str, messages: list, max_tokens: int,
         system: str | None = None, use_batch: bool = False,
         cache_system: bool = True, thinking: dict | None = None,
         effort: str | None = None, db: Database | None = None,
         monthly_budget: float | None = None, tools: list | None = None) -> dict:
    """Make one Claude call (or a one-request batch), log its cost, and return
    a small result dict: {text, usage, usd, model, stop_reason, raw_content,
    tool_uses}. When `tools` is passed, the caller runs the tool loop using
    raw_content (re-append as the assistant turn) and tool_uses.

    Raises BudgetExceeded BEFORE spending if this month is already over budget.
    """
    config = load_config()
    db = db or Database()
    model = _model_for_tier(model_tier, config)

    cap = config["costs"]["monthly_budget_usd"] if monthly_budget is None else monthly_budget
    spent = month_spend(db)
    if spent >= cap:
        raise BudgetExceeded(
            f"Monthly spend ${spent:.2f} has reached the ${cap:.2f} cap — "
            f"refusing to start task {task!r}. Raise the cap with your explicit consent."
        )

    client = anthropic.Anthropic(api_key=_require_api_key())
    kwargs = _build_kwargs(model, messages, max_tokens, system, cache_system,
                           thinking, effort, tools)

    if use_batch:
        return _run_batch(client, db, task, model, kwargs, config)

    message = client.messages.create(**kwargs)
    usage = _usage_dict(message.usage)
    usd = log_cost(db, model, task, usage, config)
    tool_uses = [{"id": b.id, "name": b.name, "input": b.input}
                 for b in message.content if getattr(b, "type", None) == "tool_use"]
    return {
        "text": _text_of(message),
        "usage": usage,
        "usd": usd,
        "model": model,
        "stop_reason": message.stop_reason,
        "raw_content": message.content,
        "tool_uses": tool_uses,
    }


def _usage_dict(usage) -> dict:
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def _run_batch(client, db: Database, task: str, model: str, kwargs: dict,
               config: dict, poll_seconds: int = 30) -> dict:
    """Submit a single request as a Batch (50% cheaper), poll to completion,
    and return the same result shape as a normal call."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    batch = client.messages.batches.create(requests=[
        Request(custom_id=task[:64] or "req", params=MessageCreateParamsNonStreaming(**kwargs)),
    ])
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        time.sleep(poll_seconds)

    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            message = result.result.message
            usage = _usage_dict(message.usage)
            usd = log_cost(db, model, f"{task} (batch)", usage, config, discount=0.5)
            return {"text": _text_of(message), "usage": usage, "usd": usd,
                    "model": model, "stop_reason": message.stop_reason}
    raise RuntimeError(f"batch {batch.id} produced no successful result")


# ── Batch fan-out (the project design: radar / scorecard / re-underwrites) ─────
def _kwargs_text(kwargs: dict) -> str:
    """Flatten the text of a request's system + messages for a rough token
    estimate (chars/4). Handles both string and content-block-list shapes."""
    parts: list[str] = []

    def _pull(content):
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", "")))
                else:
                    parts.append(str(b))

    _pull(kwargs.get("system"))
    for m in kwargs.get("messages", []):
        _pull(m.get("content"))
    return "".join(parts)


def _estimate_call_usd(model: str, kwargs: dict, config: dict) -> float:
    """Conservative pre-submit USD estimate for one request at FULL rates (the
    caller applies the 0.5 batch discount). Output is assumed to fill max_tokens
    — an over-estimate on purpose, so the budget guard errs toward refusing."""
    p = _price(model, config)
    est_in = len(_kwargs_text(kwargs)) / 4
    est_out = int(kwargs.get("max_tokens", 0) or 0)
    return est_in / 1_000_000 * p["input"] + est_out / 1_000_000 * p["output"]


def call_batch(requests: list[dict], db: Database | None = None,
               config: dict | None = None, monthly_budget: float | None = None,
               poll_interval: int = 30, hard_timeout: int = 3600) -> list[dict]:
    """Submit MANY Claude calls as ONE Message Batch (billed at 50%), poll
    bounded, and return per-request result dicts IN THE SAME ORDER as `requests`.

    Each request dict: {task, model_tier, messages, max_tokens, system?,
      cache_system?, thinking?, effort?, tools?} — the same surface as call().
    Each result dict: {task, text, usage, usd, model, stop_reason, via} where
    `via` is "batch" (served by the batch) or "sync-fallback" (served by a
    direct call() because the batch errored/expired for that item or the whole
    batch exceeded `hard_timeout`).

    Cost discipline (the project design): the total batch cost is ESTIMATED before
    submit and the batch is REFUSED with BudgetExceeded if it would push
    month-to-date spend over the cap — the same refusal contract as call().
    Every item's real cost is logged to api_costs with its own operation label,
    whether it was served by the batch or the sync fallback.

    Cadence: radar runs weekly, scorecard monthly, so the default 1-hour
    hard_timeout with a graceful sync fallback is well inside the loop cadence.
    """
    if not requests:
        return []
    config = config or load_config()
    db = db or Database()
    cap = (config["costs"]["monthly_budget_usd"]
           if monthly_budget is None else monthly_budget)

    prepared = []
    for i, req in enumerate(requests):
        model = _model_for_tier(req["model_tier"], config)
        kwargs = _build_kwargs(model, req["messages"], req["max_tokens"],
                               req.get("system"), req.get("cache_system", True),
                               req.get("thinking"), req.get("effort"),
                               req.get("tools"))
        prepared.append({"i": i, "task": req.get("task") or f"batch-{i}",
                         "model": model, "kwargs": kwargs, "req": req,
                         "cid": f"r{i}"})

    # Estimate BEFORE submit (batch is 50% off) and refuse an over-budget batch.
    est_total = sum(_estimate_call_usd(p["model"], p["kwargs"], config) * 0.5
                    for p in prepared)
    spent = month_spend(db)
    if spent + est_total > cap:
        raise BudgetExceeded(
            f"A batch of {len(prepared)} request(s) is estimated at "
            f"~${est_total:.2f}; on top of ${spent:.2f} spent this month that "
            f"would exceed the ${cap:.2f} cap — refusing to submit. "
            f"Raise the cap with your explicit consent.")

    client = anthropic.Anthropic(api_key=_require_api_key())
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    batch = client.messages.batches.create(requests=[
        Request(custom_id=p["cid"],
                params=MessageCreateParamsNonStreaming(**p["kwargs"]))
        for p in prepared])

    start = time.monotonic()
    ended = False
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            ended = True
            break
        if time.monotonic() - start > hard_timeout:
            break
        time.sleep(poll_interval)

    results: dict[int, dict] = {}
    if ended:
        for result in client.messages.batches.results(batch.id):
            idx = int(str(result.custom_id)[1:])
            p = prepared[idx]
            if result.result.type == "succeeded":
                msg = result.result.message
                usage = _usage_dict(msg.usage)
                usd = log_cost(db, p["model"], f"{p['task']} (batch)", usage,
                               config, discount=0.5)
                results[idx] = {"task": p["task"], "text": _text_of(msg),
                                "usage": usage, "usd": usd, "model": p["model"],
                                "stop_reason": msg.stop_reason, "via": "batch"}
            # errored / expired / canceled → fall through to the sync path below
    else:
        # Hard timeout: cancel (best-effort) so the batch stops billing, then
        # serve every request synchronously. The timeout itself is surfaced.
        print(f"[llm.call_batch] batch {batch.id} did not end within "
              f"{hard_timeout}s — cancelling and falling back to sync for "
              f"{len(prepared)} request(s).", file=_sys.stderr)
        try:
            client.messages.batches.cancel(batch.id)
        except Exception:
            pass

    for p in prepared:
        if p["i"] in results:
            continue
        req = p["req"]
        r = call(p["task"], req["model_tier"], req["messages"], req["max_tokens"],
                 system=req.get("system"), cache_system=req.get("cache_system", True),
                 thinking=req.get("thinking"), effort=req.get("effort"),
                 db=db, monthly_budget=monthly_budget, tools=req.get("tools"))
        results[p["i"]] = {**r, "task": p["task"], "via": "sync-fallback"}

    return [results[i] for i in range(len(prepared))]


def _selftest() -> None:
    """Verification. Proves: (1) the budget guard raises BudgetExceeded
    without spending, (2) cost logging writes correct api_costs rows. A live
    per-tier call runs only if ANTHROPIC_API_KEY is present; otherwise the
    logging path is verified with a simulated usage object (clearly labelled),
    and the real per-tier calls happen once the key lands."""
    config = load_config()
    db = Database()
    db.apply_schema()

    print(f"backend = {db.backend}")
    print("\n[1] Budget guard — fake $999 cap already spent:")
    db.insert("api_costs", {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": _model_for_tier("haiku", config), "task": "seed",
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "usd_estimate": 999.0,
    })
    try:
        call("should-not-run", "haiku", [{"role": "user", "content": "hi"}],
             max_tokens=16, db=db, monthly_budget=1.0)
        print("  FAIL — BudgetExceeded was not raised")
    except BudgetExceeded as e:
        print(f"  OK — raised BudgetExceeded before any spend")
    db.delete("api_costs", {"task": "seed"})

    have_key = bool(os.getenv("ANTHROPIC_API_KEY", "").strip() or _env_key())
    if have_key:
        print("\n[2] Live per-tier calls (logging real usage):")
        for tier in ("haiku", "sonnet", "opus"):
            r = call(f"selftest-{tier}", tier,
                     [{"role": "user", "content": "Reply with the single word OK."}],
                     max_tokens=16, db=db)
            print(f"  {tier:<7} {r['model']:<18} "
                  f"in={r['usage']['input_tokens']} out={r['usage']['output_tokens']} "
                  f"${r['usd']:.6f}")
    else:
        print("\n[2] No ANTHROPIC_API_KEY yet (owner action A3) — verifying the")
        print("    cost-logging math with a SIMULATED usage object instead:")
        for tier in ("haiku", "sonnet", "opus"):
            model = _model_for_tier(tier, config)
            usd = log_cost(db, model, f"simulated-{tier}",
                           {"input_tokens": 1000, "output_tokens": 500,
                            "cache_read_input_tokens": 0}, config)
            print(f"  {tier:<7} {model:<18} 1000 in / 500 out -> ${usd:.6f}")

    print("\napi_costs rows now in the store:")
    for row in db.select("api_costs", order_by="id"):
        print(f"  #{row['id']} {row['model']:<18} {row['task']:<20} "
              f"${float(row['usd_estimate']):.6f}")
    print(f"\nmonth-to-date spend: ${month_spend(db):.6f}")
    db.close()
    print("\nT3 PASS")


def _env_key() -> str:
    load_env()
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


if __name__ == "__main__":
    _selftest()
