"""scout/prompts_lint.py — lint the prompt library.

Confirms, for every prompts/*.md template:
  1. the metadata header states model_tier, token_budget, and checkers,
  2. every {{PLACEHOLDER}} is one the orchestrator knows how to fill (resolves).

(The exemplar prompts ship with a neutral placeholder exemplar; the verbatim-
embed check that once pinned them to a private validation set is intentionally not
enforced in the public build.)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"

VALID_TIERS = {"opus", "sonnet", "haiku"}
KNOWN_CHECKERS = {"citation", "evidence_dating", "arithmetic", "banned_phrase"}

# Placeholders the orchestrator knows how to fill. A {{TOKEN}} outside this
# set means the template would not resolve at run time.
ALLOWED_PLACEHOLDERS = {
    "SYMBOL", "COMPANY", "AS_OF_DATE", "CUTOFF_CLAUSE", "EDGAR_USER_AGENT",
    "EVIDENCE_PACK", "UNDERWRITE_BRIEF", "THESIS", "BREAK_CONDITIONS",
    "ENTRY_TRIGGERS", "NEW_SIGNALS", "FEED_ITEMS",
    # head-to-head compare (Task 10)
    "SYMBOL_A", "SYMBOL_B", "PACK_A", "PACK_B",
}

# prompt file -> exemplar it must embed verbatim. Empty in the public build: the
# shipped prompts carry a neutral placeholder exemplar instead of a private one.
EXEMPLARS: dict[str, object] = {}

HEADER_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _parse_header(text: str) -> dict:
    m = HEADER_RE.search(text)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta


def lint() -> int:
    files = sorted(PROMPTS_DIR.glob("*.md"))
    if not files:
        print(f"no prompt files found in {PROMPTS_DIR}")
        return 1

    errors = 0
    print(f"{'file':<20} {'tier':<7} {'budget':<7} checkers / placeholders")
    print("-" * 78)
    for f in files:
        text = f.read_text()
        meta = _parse_header(text)
        problems = []

        tier = meta.get("model_tier", "")
        if tier not in VALID_TIERS:
            problems.append(f"bad/missing model_tier {tier!r}")
        budget = meta.get("token_budget", "")
        if not budget.isdigit():
            problems.append(f"bad/missing token_budget {budget!r}")
        checkers = [c.strip() for c in meta.get("checkers", "").split(",") if c.strip()]
        if not checkers:
            problems.append("missing checkers")
        for c in checkers:
            if c not in KNOWN_CHECKERS:
                problems.append(f"unknown checker {c!r}")

        placeholders = set(PLACEHOLDER_RE.findall(text))
        unknown = placeholders - ALLOWED_PLACEHOLDERS
        if unknown:
            problems.append(f"unresolvable placeholder(s): {sorted(unknown)}")

        if f.name in EXEMPLARS:
            exemplar = EXEMPLARS[f.name].read_text().strip()
            if exemplar not in text:
                problems.append(f"does NOT embed {EXEMPLARS[f.name].name} verbatim")

        status = "OK" if not problems else "FAIL"
        ph = ",".join(sorted(placeholders)) or "(none)"
        print(f"{f.name:<20} {tier:<7} {budget:<7} [{','.join(checkers)}] {{{ph}}}")
        if problems:
            for p in problems:
                print(f"    ✗ {p}")
            errors += 1

    print("-" * 78)
    if errors:
        print(f"LINT FAIL — {errors} file(s) with problems")
        return 1
    print(f"LINT OK — {len(files)} prompt files; all headers valid, all "
          f"placeholders resolvable, exemplars embedded verbatim")
    return 0


if __name__ == "__main__":
    sys.exit(lint())
