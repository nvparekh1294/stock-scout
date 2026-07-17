# Maintenance

## The port rule: this repo is downstream of a private build

Stock Scout's public repository is **downstream-only** of the owner's private
development. New work happens in the private project first and is ported here after a
sanitization pass; changes do not flow the other way. Concretely:

- When a ported module changes in the private project, the port re-runs the same
  content sanitize + gate (personal-identifier scan, private-ticker scan, and a
  written review) before the change lands in this public repo.
- If the port is deferred, the public repo simply trails the private build for a
  while — it never contains un-sanitized content.

## Monthly drift check (about 5 minutes)

Once a month, confirm the public repo has not drifted from the private build in a way
that matters, and specifically:

1. **Pricing table currency.** Verify `costs.pricing` in `config.yml` /
   `config.example.yml` against the [Anthropic pricing page](https://www.anthropic.com/pricing)
   and update `costs.pricing_as_of` (`YYYY-MM`). The app prints a plain staleness
   warning at boot and in every cost report once this date is more than four months
   old — treat that warning as the reminder if you miss the monthly pass. The README's
   dated cost ranges ("at July 2026 pricing") should be refreshed at the same time.
2. **Sanitization gate.** Re-run the personal-identifier and private-ticker scans over
   the full tree (and the pre-commit hook stays enabled) so nothing personal has crept
   in.
3. **Suite + checkers.** `python -m pytest` green, and the deterministic checkers still
   pass on `briefs.example/NRDX_underwrite_EXAMPLE.md`.

If the port has been deferred for more than two months, note in the README that the
public repo trails the private build.
