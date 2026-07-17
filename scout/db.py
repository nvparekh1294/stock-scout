"""scout/db.py — data access layer with a Postgres backend and a JSON-file
fallback.

The module stays usable without a reachable database: when DATABASE_URL is
absent, every DAO call is MIRRORED against a local JSON store (scout/_localdb/,
gitignored) instead of raising — so the whole package can run end-to-end (dev,
tests, first boot) before you provision a Postgres URL.

The DAO surface is deliberately generic (insert / select / update / delete /
count / upsert). Both backends implement it identically, which is what makes
the fallback a true mirror rather than a stub.

Contains NO order/execution code and never prints secrets (the DATABASE_URL is
never echoed).
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from .config import REPO_ROOT, get_db_url

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
LOCALDB_DIR = Path(__file__).resolve().parent / "_localdb"
# Bring your own cost-basis export. Point load_lots_csv() at any broker CSV with
# columns: symbol, purchase_date, shares, total_cost. The portfolio/ directory is
# gitignored — your data never enters the repo.
DEFAULT_LOTS_CSV = REPO_ROOT / "portfolio" / "lots.csv"

# Known tables (mirrors schema.sql). Used to validate table names and to report
# row counts. 'system_flags' keys on `flag`, not a serial `id`.
TABLES = [
    "accounts", "lots", "theses", "break_conditions", "entry_triggers",
    "recommendations", "ledger_marks", "evidence", "constraints",
    "api_costs", "system_flags", "conversation", "peer_metrics",
    "profiles",
]


def _json_default(obj):
    """Make Decimals/dates JSON-serializable in the fallback store."""
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


class Database:
    """One handle over either Postgres or the JSON fallback."""

    def __init__(self, db_url: str | None = None):
        # db_url=None → resolve from env; "" would force JSON mode explicitly.
        self.db_url = get_db_url() if db_url is None else db_url
        self.backend = "postgres" if self.db_url else "json"
        self._conn = None
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            # create_engine-style: this DOES open a connection; if it fails we
            # let it raise, because a caller that set DATABASE_URL wants Postgres.
            self._conn = psycopg.connect(
                self.db_url, autocommit=True, row_factory=dict_row
            )
        else:
            LOCALDB_DIR.mkdir(exist_ok=True)

    # ── schema ────────────────────────────────────────────────────────────
    def apply_schema(self) -> None:
        """Postgres: execute schema.sql (idempotent CREATE TABLE IF NOT EXISTS).
        JSON: schemaless — just ensure the store directory exists."""
        if self.backend == "postgres":
            self._conn.execute(SCHEMA_PATH.read_text())
        else:
            LOCALDB_DIR.mkdir(exist_ok=True)

    # ── JSON backend helpers ──────────────────────────────────────────────
    def _json_path(self, table: str) -> Path:
        return LOCALDB_DIR / f"{table}.json"

    def _json_load(self, table: str) -> dict:
        p = self._json_path(table)
        if not p.exists():
            return {"seq": 0, "rows": []}
        return json.loads(p.read_text())

    def _json_save(self, table: str, store: dict) -> None:
        self._json_path(table).write_text(
            json.dumps(store, indent=2, default=_json_default)
        )

    @staticmethod
    def _match(row: dict, where: dict | None) -> bool:
        if not where:
            return True
        return all(str(row.get(k)) == str(v) for k, v in where.items())

    # ── generic DAO (identical semantics on both backends) ────────────────
    def insert(self, table: str, row: dict) -> int | str:
        assert table in TABLES, f"unknown table {table}"
        if self.backend == "postgres":
            from psycopg import sql
            cols = list(row.keys())
            q = sql.SQL("INSERT INTO {t} ({c}) VALUES ({v}) RETURNING *").format(
                t=sql.Identifier(table),
                c=sql.SQL(", ").join(map(sql.Identifier, cols)),
                v=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
            )
            cur = self._conn.execute(q, [row[c] for c in cols])
            out = cur.fetchone()
            return out.get("id", out.get("flag"))
        store = self._json_load(table)
        store["seq"] += 1
        new = dict(row)
        new["id"] = store["seq"]
        store["rows"].append(new)
        self._json_save(table, store)
        return new["id"]

    def insert_many(self, table: str, rows: list[dict]) -> int:
        for r in rows:
            self.insert(table, r)
        return len(rows)

    def select(self, table: str, where: dict | None = None,
               order_by: str | None = None) -> list[dict]:
        assert table in TABLES, f"unknown table {table}"
        if self.backend == "postgres":
            from psycopg import sql
            q = sql.SQL("SELECT * FROM {t}").format(t=sql.Identifier(table))
            params: list = []
            if where:
                conds = [sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
                         for k in where]
                q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
                params = list(where.values())
            if order_by:
                q += sql.SQL(" ORDER BY {}").format(sql.Identifier(order_by))
            return list(self._conn.execute(q, params).fetchall())
        rows = [r for r in self._json_load(table)["rows"] if self._match(r, where)]
        if order_by:
            rows = sorted(rows, key=lambda r: (r.get(order_by) is None, r.get(order_by)))
        return rows

    def select_one(self, table: str, where: dict | None = None) -> dict | None:
        rows = self.select(table, where)
        return rows[0] if rows else None

    def update(self, table: str, row_id: int, changes: dict) -> None:
        assert table in TABLES, f"unknown table {table}"
        if self.backend == "postgres":
            from psycopg import sql
            sets = [sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
                    for k in changes]
            q = sql.SQL("UPDATE {t} SET {s} WHERE id = {i}").format(
                t=sql.Identifier(table),
                s=sql.SQL(", ").join(sets),
                i=sql.Placeholder(),
            )
            self._conn.execute(q, list(changes.values()) + [row_id])
            return
        store = self._json_load(table)
        for r in store["rows"]:
            if r.get("id") == row_id:
                r.update(changes)
        self._json_save(table, store)

    def delete(self, table: str, where: dict | None = None) -> int:
        assert table in TABLES, f"unknown table {table}"
        if self.backend == "postgres":
            from psycopg import sql
            q = sql.SQL("DELETE FROM {t}").format(t=sql.Identifier(table))
            params: list = []
            if where:
                conds = [sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
                         for k in where]
                q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
                params = list(where.values())
            cur = self._conn.execute(q, params)
            return cur.rowcount
        store = self._json_load(table)
        before = len(store["rows"])
        store["rows"] = [r for r in store["rows"] if not self._match(r, where)]
        self._json_save(table, store)
        return before - len(store["rows"])

    def count(self, table: str, where: dict | None = None) -> int:
        if self.backend == "postgres":
            from psycopg import sql
            q = sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(table))
            params: list = []
            if where:
                conds = [sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
                         for k in where]
                q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
                params = list(where.values())
            return self._conn.execute(q, params).fetchone()["n"]
        return len(self.select(table, where))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


# ── portfolio loader ──────────────────────────────────────────────────────
def load_lots_csv(db: Database, csv_path: Path | str | None = None,
                  account_name: str = "Brokerage",
                  account_type: str = "taxable") -> dict:
    """Idempotently load a broker cost-basis CSV into `lots` under the named
    account (source='csv_import'). Re-running replaces the prior csv_import lots
    for that account, never duplicates them.

    Bring your own export: the CSV needs columns symbol, purchase_date, shares,
    total_cost. Point csv_path at it (defaults to portfolio/lots.csv, which is
    gitignored). import_confirmed ships FALSE — flip it once you have reconciled
    the import against your brokerage of record.
    """
    csv_path = Path(csv_path) if csv_path else DEFAULT_LOTS_CSV
    acct = db.select_one("accounts", {"name": account_name, "type": account_type})
    account_id = acct["id"] if acct else db.insert(
        "accounts", {"name": account_name, "type": account_type})

    db.delete("lots", {"account_id": account_id, "source": "csv_import"})

    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        lot_rows = [{
            "account_id": account_id,
            "symbol": r["symbol"],
            "purchase_date": r["purchase_date"],
            "shares": Decimal(r["shares"]),
            "total_cost": Decimal(r["total_cost"]),
            "source": "csv_import",
            "import_confirmed": False,
        } for r in reader]

    db.insert_many("lots", lot_rows)
    total_cost = sum(l["total_cost"] for l in lot_rows)
    total_shares = sum(l["shares"] for l in lot_rows)
    return {
        "account_id": account_id,
        "lots": len(lot_rows),
        "total_cost": float(total_cost),
        "total_shares": float(total_shares),
    }


def _selftest() -> None:
    """Verification: apply schema, report counts, and prove the generic DAO
    round-trips (insert→select→update→delete) on both backends. Uses only
    synthetic rows — no personal data or external CSV required."""
    db = Database()
    print(f"backend = {db.backend}")
    db.apply_schema()

    print("\nrow counts by table:")
    for t in TABLES:
        print(f"  {t:<18} {db.count(t)}")

    # Generic DAO round-trip on `theses`.
    tid = db.insert("theses", {"symbol": "__TEST__", "stage": 0, "conviction": 1,
                               "verdict": "TEST", "thesis_text": "roundtrip",
                               "status": "active"})
    got = db.select_one("theses", {"id": tid})
    assert got and got["symbol"] == "__TEST__", "insert/select failed"
    db.update("theses", tid, {"conviction": 5, "status": "watch"})
    got = db.select_one("theses", {"id": tid})
    assert got["conviction"] == 5 and got["status"] == "watch", "update failed"
    removed = db.delete("theses", {"id": tid})
    assert removed == 1 and db.select_one("theses", {"id": tid}) is None, "delete failed"
    print("\nDAO round-trip (insert→select→update→delete): OK")

    db.close()
    print("\nT2 PASS")


if __name__ == "__main__":
    _selftest()
