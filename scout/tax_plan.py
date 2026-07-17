"""scout/tax_plan.py — specific-lot, config-driven tax-sell planner.

Given a target dollar amount to raise, ranks taxable lots by tax cost per dollar
raised and builds a specific-lot sell plan: per-lot gain, estimated tax, and the
running total. Advice only — you execute in your own brokerage. Deterministic;
the only network call is a delayed price fetch.

FAIL CLOSED ON JURISDICTION: there is NO default country or state and NO built-in
rate table. The planner REFUSES to run unless config.yml sets a US jurisdiction
and the tax rates it needs — it never silently assumes a jurisdiction or invents
a rate. Non-US jurisdictions are refused because the arithmetic here models the
US long-term/short-term capital-gains distinction only; adapt it deliberately for
anywhere else rather than trusting numbers that don't apply to you.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from .config import load_config
from .db import Database


class TaxConfigError(RuntimeError):
    """Raised when the tax configuration is unset, non-US, or missing a rate —
    the planner refuses rather than assume anything."""


def _resolved_rates(db: Database | None = None) -> dict:
    """Read and validate the tax block from config. HARD-REFUSES (TaxConfigError)
    when the jurisdiction is unset or not US, or when a required rate is missing.
    Never falls back to a default jurisdiction or a default rate.

    A confirmed investor profile's jurisdiction WINS over config's (the profile
    is where a user states their country + US state during first-run intake). The
    RATES still come from config — the plain-language interview never collects
    numeric tax rates, so an unset rate still refuses even with a profile country.
    When no db/profile is given, behavior is exactly the config-only path."""
    tax = (load_config() or {}).get("tax") or {}
    jur_src = str(tax.get("jurisdiction") or "").strip()
    state_src = str(tax.get("state") or "").strip()
    if db is not None:
        from .profile import confirmed_profile
        prof = confirmed_profile(db)
        if prof:
            if str(prof.get("tax_country") or "").strip():
                jur_src = str(prof["tax_country"]).strip()
            if str(prof.get("tax_state") or "").strip():
                state_src = str(prof["tax_state"]).strip()
    jur = jur_src.upper()
    if not jur:
        raise TaxConfigError(
            "No tax jurisdiction is configured. Set `tax.jurisdiction` in "
            "config.yml (this planner models US capital-gains rules only) and "
            "the applicable rates before using any tax feature. The planner "
            "refuses to assume a jurisdiction.")
    if jur not in ("US", "USA", "UNITED STATES"):
        raise TaxConfigError(
            f"Tax jurisdiction {jur!r} is not supported. This planner models the "
            "US long-term/short-term capital-gains distinction only and will not "
            "apply US math to another jurisdiction. Configure a US jurisdiction "
            "or adapt the planner deliberately for your country.")

    def _rate(key: str, required: bool) -> float:
        v = tax.get(key)
        if v is None or str(v).strip() == "":
            if required:
                raise TaxConfigError(
                    f"Tax rate `tax.{key}` is unset in config.yml. Set your own "
                    "rate — the planner never assumes one.")
            return 0.0
        return float(v)

    long_term_only = bool(tax.get("long_term_only", False))
    return {
        "jurisdiction": jur,
        "federal_lt": _rate("federal_lt_rate", required=True),
        # ST rate only required if short-term lots may be included.
        "federal_st": _rate("federal_st_rate", required=not long_term_only),
        "state_rate": _rate("state_rate", required=False),
        "surtax_rate": _rate("surtax_rate", required=False),
        "state": state_src,
        "long_term_only": long_term_only,
    }


def _spot(symbol: str) -> float | None:
    from .gather import _alpaca_price_range
    px = _alpaca_price_range(symbol)
    return None if "error" in px else float(px["latest_close"])


def build_plan(db: Database, target_usd: float,
               account_name: str | None = None) -> dict:
    """Build a specific-lot sell plan to raise ~target_usd, cheapest tax-per-
    dollar first. Rates come entirely from config (US jurisdiction required); the
    jurisdiction may come from a confirmed investor profile (profile wins)."""
    r = _resolved_rates(db)
    fed_lt, fed_st = r["federal_lt"], r["federal_st"]
    add_on = r["state_rate"] + r["surtax_rate"]
    lt_cutoff = (date.today() - timedelta(days=366)).isoformat()

    lots = db.select("lots", order_by="id")
    if account_name:
        accts = {a["id"] for a in db.select("accounts", {"name": account_name})}
        lots = [l for l in lots if l["account_id"] in accts]
    taxable_ids = {a["id"] for a in db.select("accounts", {"type": "taxable"})}
    lots = [l for l in lots if l["account_id"] in taxable_ids]

    eligible = []
    skipped_st = 0
    for l in lots:
        is_lt = str(l["purchase_date"])[:10] <= lt_cutoff
        if not is_lt and r["long_term_only"]:
            skipped_st += 1
            continue
        eligible.append((l, is_lt))

    prices: dict[str, float | None] = {}
    rows = []
    for l, is_lt in eligible:
        sym = l["symbol"]
        if sym not in prices:
            prices[sym] = _spot(sym)
        px = prices[sym]
        if px is None:
            continue
        shares = float(l["shares"])
        basis = float(l["total_cost"])
        value = shares * px
        gain = value - basis
        marginal = (fed_lt if is_lt else fed_st) + add_on
        tax = max(0.0, gain) * marginal
        rows.append({"lot_id": l["id"], "symbol": sym,
                     "purchase_date": str(l["purchase_date"])[:10],
                     "term": "LT" if is_lt else "ST",
                     "shares": shares, "basis": basis, "value": value,
                     "gain": gain, "est_tax": tax,
                     "tax_per_dollar": tax / value if value else 9e9})

    # Cheapest tax per dollar raised first (losses first, then smallest gains).
    rows.sort(key=lambda r: r["tax_per_dollar"])
    plan, raised, total_tax = [], 0.0, 0.0
    for row in rows:
        if raised >= target_usd:
            break
        plan.append(row)
        raised += row["value"]
        total_tax += row["est_tax"]

    unpriced = sorted({s for s, p in prices.items() if p is None})
    return {"target": target_usd, "raised": raised, "est_tax": total_tax,
            "lots": plan, "n_eligible": len(rows), "skipped_st": skipped_st,
            "unpriced": unpriced,
            "rates": {"federal_lt": fed_lt, "federal_st": fed_st,
                      "state_rate": r["state_rate"], "surtax_rate": r["surtax_rate"],
                      "state": r["state"], "long_term_only": r["long_term_only"]}}


def render_plan(p: dict) -> str:
    r = p["rates"]
    state_note = (f" + state {r['state_rate']*100:.1f}%" if r["state_rate"] else "")
    surtax_note = (f" + surtax {r['surtax_rate']*100:.1f}%" if r["surtax_rate"] else "")
    st_note = ("" if r["long_term_only"]
               else f" / ST {r['federal_st']*100:.0f}%")
    lt_note = "long-term lots only" if r["long_term_only"] else "long- and short-term lots"
    lines = [
        f"# Specific-lot sell plan — raise ~${p['target']:,.0f}",
        f"*{lt_note}, taxable accounts, lots ranked by tax cost per dollar "
        f"raised. Rates from your config: federal LT {r['federal_lt']*100:.0f}%"
        f"{st_note}{state_note}{surtax_note}. Delayed prices. Advice only; you "
        f"execute; confirm lot IDs at your brokerage.*", "",
        "| Lot | Symbol | Bought | Term | Shares | Basis | Value (delayed) | Gain | Est. tax |",
        "|---|---|---|---|---|---|---|---|---|"]
    for l in p["lots"]:
        lines.append(f"| {l['lot_id']} | {l['symbol']} | {l['purchase_date']} | "
                     f"{l['term']} | {l['shares']:.4f} | ${l['basis']:,.2f} | "
                     f"${l['value']:,.2f} | ${l['gain']:+,.2f} | ${l['est_tax']:,.2f} |")
    lines += ["",
              f"**Raises ${p['raised']:,.2f}** against the ${p['target']:,.0f} target "
              f"across {len(p['lots'])} lots · **estimated total tax ${p['est_tax']:,.2f}** "
              f"(effective {p['est_tax']/p['raised']*100 if p['raised'] else 0:.1f}% of proceeds).",
              f"- {p['n_eligible']} eligible lots were priced this run; "
              f"{p['skipped_st']} short-term lots were excluded by the long-term-only rule."]
    if p["unpriced"]:
        lines.append(f"- NOT PRICED this run (no delayed quote): "
                     f"{', '.join(p['unpriced'])} — their lots were left out.")
    if p["raised"] < p["target"]:
        lines.append(f"- ⚠️ Eligible lots cover only ${p['raised']:,.2f} of the "
                     f"target. The gap needs additional lots or a different account.")
    lines.append("- Wash-sale note: only relevant if repurchasing within 30 days "
                 "— flag your intent before rebuying anything sold at a loss.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Specific-lot tax-sell planner")
    ap.add_argument("target", type=float, help="dollars to raise")
    ap.add_argument("--account", default=None, help="limit to one broker name")
    args = ap.parse_args()
    db = Database()
    db.apply_schema()
    try:
        print(render_plan(build_plan(db, args.target, args.account)))
    except TaxConfigError as e:
        raise SystemExit(f"Tax planner refused: {e}")


if __name__ == "__main__":
    main()
