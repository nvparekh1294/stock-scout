"""scout/migrate.py — one-time seed of Postgres from the JSON fallback store.

The P0/P1 build ran on the JSON fallback (DATABASE_URL was empty). This seeds
Postgres from those files EXACTLY ONCE: a table is seeded only if it is empty
in Postgres, so re-runs and restarts are no-ops and Postgres stays the single
source of truth afterwards (no split-brain).

Foreign keys are remapped (JSON ids → fresh Postgres serials) in dependency
order. Runs harmlessly when DATABASE_URL is absent (JSON mode needs no seed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import get_db_url
from .db import Database, TABLES


def _load(seed_dir: Path, table: str) -> list[dict]:
    p = seed_dir / f"{table}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("rows", [])


# ── legacy-collision healing ────────────────────────────────────────────────
# A shared Postgres may already hold tables from another application. Where a
# table NAME collides but the COLUMNS don't match this schema (e.g. a foreign
# api_costs with no "ts" column), the app must not write into the legacy table.
# Heal by RENAMING the legacy table aside
# (nothing is deleted; reversible with a one-line RENAME back), then letting
# apply_schema() recreate the correct one so seeding can proceed.
import re as _re

LEGACY_SUFFIX = "_legacy_collision"


def _schema_columns() -> dict[str, set[str]]:
    """Parse schema.sql → {table: {column names}} (deterministic, no DB)."""
    from .db import SCHEMA_PATH
    out: dict[str, set[str]] = {}
    sql = SCHEMA_PATH.read_text()
    for m in _re.finditer(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\);", sql, _re.S):
        table, body = m.group(1), m.group(2)
        cols = set()
        for line in body.splitlines():
            first = line.strip().split(" ")[0].strip(", ").lower()
            # Only a bare SQL identifier counts as a column. Continuation lines
            # of a multi-line constraint (e.g. the quadrant CHECK value list)
            # start with '(' and previously leaked in as a phantom "column",
            # making a healthy table look incompatible forever (2026-07-12 bug).
            if (_re.fullmatch(r"[a-z_][a-z0-9_]*", first)
                    and first not in ("unique", "check", "primary", "foreign",
                                      "constraint")):
                cols.add(first)
        out[table] = cols
    return out


def _table_columns(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()
    return {r["column_name"].lower() for r in rows}


def heal_legacy_collisions(db: Database) -> list[str]:
    """Postgres only: rename any existing table whose columns are missing part
    of Scout's schema, then re-apply the schema. Returns the actions taken."""
    if db.backend != "postgres":
        return []
    actions = []
    schema = _schema_columns()

    # Recovery from the 2026-07-12 phantom-column bug: if a *_legacy table's
    # columns MATCH Scout's schema, it is Scout's own table that was wrongly
    # renamed — restore it over the (bug-reseeded) current one. A genuinely
    # foreign legacy table (e.g. another application's api_costs) never matches
    # and is never touched.
    for table, expected in schema.items():
        legacy = f"{table}{LEGACY_SUFFIX}"
        legacy_cols = _table_columns(db, legacy)
        if legacy_cols and expected <= legacy_cols:
            cur_cols = _table_columns(db, table)
            if cur_cols:
                db._conn.execute(f'DROP TABLE "{table}"')
            db._conn.execute(f'ALTER TABLE "{legacy}" RENAME TO "{table}"')
            actions.append(f"restored {legacy} -> {table} (it was Scout's own "
                           f"table, renamed by the 7/12 parser bug)")

    for table, expected in schema.items():
        have = _table_columns(db, table)
        if not have:
            continue  # doesn't exist yet — apply_schema will create it
        missing = expected - have
        if missing:
            legacy = f"{table}{LEGACY_SUFFIX}"
            if _table_columns(db, legacy):
                actions.append(f"NOTE: {table} still mismatched but {legacy} "
                               f"already exists — leaving both for manual review")
                continue
            db._conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')
            actions.append(f"renamed incompatible table {table} -> {legacy} "
                           f"(missing columns: {', '.join(sorted(missing))}); "
                           f"legacy data preserved")
    db.apply_schema()
    return actions


def seed(db: Database, seed_dir: Path) -> dict:
    done = {}
    acct_map, thesis_map, rec_map = {}, {}, {}

    def _seed(table: str, transform=None) -> int:
        if db.count(table) > 0:
            done[table] = "skipped (has rows)"
            return 0
        n = 0
        for row in _load(seed_dir, table):
            row = dict(row)
            old_id = row.pop("id", None)
            if transform and transform(row, old_id) is False:
                continue
            new_id = db.insert(table, row)
            if table == "accounts":
                acct_map[old_id] = new_id
            elif table == "theses":
                thesis_map[old_id] = new_id
            elif table == "recommendations":
                rec_map[old_id] = new_id
            n += 1
        done[table] = f"seeded {n}"
        return n

    _seed("accounts")
    _seed("theses")
    _seed("lots", lambda r, _: (r.update(account_id=acct_map.get(r.get("account_id"),
                                                                 r.get("account_id"))) or True))
    for child in ("break_conditions", "entry_triggers"):
        _seed(child, lambda r, _: (thesis_map.get(r.get("thesis_id")) is not None
                                   and (r.update(thesis_id=thesis_map[r["thesis_id"]]) or True)))
    _seed("recommendations", lambda r, _: (r.update(
        thesis_id=thesis_map.get(r.get("thesis_id"))) or True))
    _seed("ledger_marks", lambda r, _: (rec_map.get(r.get("recommendation_id")) is not None
                                        and (r.update(recommendation_id=rec_map[r["recommendation_id"]]) or True)))
    for plain in ("evidence", "constraints", "api_costs", "conversation",
                  "profiles"):
        _seed(plain)
    _seed("system_flags", lambda r, _: True)
    return done


def main():
    ap = argparse.ArgumentParser(description="Seed Postgres from the JSON store (once)")
    ap.add_argument("--seed-dir", default="scout/_localdb")
    args = ap.parse_args()
    if not get_db_url():
        print("migrate: DATABASE_URL not set — JSON mode, nothing to seed. OK.")
        return
    seed_dir = Path(args.seed_dir)
    if not seed_dir.exists():
        print(f"migrate: no seed dir {seed_dir} — nothing to do. OK.")
        return
    db = Database()
    db.apply_schema()
    for action in heal_legacy_collisions(db):
        print(f"  HEAL: {action}")
    for table, status in seed(db, seed_dir).items():
        print(f"  {table:<18} {status}")
    print("migrate: done. Postgres is now the single source of truth.")
    db.close()


if __name__ == "__main__":
    main()
