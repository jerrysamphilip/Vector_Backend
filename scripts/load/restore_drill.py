"""Backup / restore drill on the LOCAL docker MySQL (chunk V2; docs/OPERATIONS.md §7 "Restore drill").

    MYSQL_HOST=127.0.0.1 MYSQL_PORT=3316 MYSQL_USER=root MYSQL_PASSWORD=root \\
    python scripts/load/restore_drill.py --source vtest_load [--target vtest_load_restore] [--keep]

Steps, each timed: (1) mysqldump the source inside the MySQL container (--single-transaction, routines,
triggers), (2) drop and recreate the target with the source's charset/collation, (3) restore the dump into
it, (4) `alembic current` against the target and compare with the source's alembic_version, (5) exact row
counts per table, source vs target, (6) an order-independent content checksum per table, (7) invariants on the target: no foreign-key
orphans, no duplicates in any unique index, same tables / indexes / foreign keys / generated columns as the
source. Prints a summary and writes reports/load/restore_drill_<ts>.{json,md}. Exit 1 when any check fails.

This rehearses the mechanics and gives the timings per 100k contacts; the production restore is RDS PITR
(OPERATIONS.md §7), which this cannot exercise. Only database names starting with vtest_load or load_ are
touched, never outreach_ai, and only through `docker exec` on the named local container and a connection to
127.0.0.1 / localhost.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - fixed argv lists, no shell
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pymysql

ROOT = Path(__file__).resolve().parents[2]
DB_NAME_RE = re.compile(r"^(vtest_load|load_)[A-Za-z0-9_]*$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_names(source: str, target: str, host: str) -> Optional[str]:
    """Why the drill must not run with these settings, or None."""
    for name in (source, target):
        if name == "outreach_ai" or not DB_NAME_RE.match(name or ""):
            return f"refusing: {name!r} is not a load-test database (vtest_load* / load_*)"
    if source == target:
        return "refusing: target must differ from source"
    if host not in LOCAL_HOSTS:
        return f"refusing: MYSQL_HOST={host} is not the local docker MySQL"
    return None


@dataclass
class Drill:
    args: argparse.Namespace
    steps: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.perf_counter()
        print(f"-> {name} ...", flush=True)
        result = fn()
        took = time.perf_counter() - t0
        self.steps.append({"step": name, "seconds": round(took, 2), "detail": result if isinstance(result, str) else ""})
        print(f"   {took:.1f}s {result if isinstance(result, str) else ''}", flush=True)
        return result

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"   [{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


# ── MySQL access ────────────────────────────────────────────────────────────

def connect(db: Optional[str] = None) -> pymysql.connections.Connection:
    return pymysql.connect(host=os.environ.get("MYSQL_HOST", "127.0.0.1"), port=int(os.environ.get("MYSQL_PORT", "3316")),
                           user=os.environ.get("MYSQL_USER", "root"), password=os.environ.get("MYSQL_PASSWORD", ""),
                           database=db, autocommit=True, charset="utf8mb4")


_CONNECTIONS: Dict[Optional[str], pymysql.connections.Connection] = {}


def query(db: Optional[str], sql: str, params: Tuple = ()) -> List[Tuple]:
    """Run one statement on a cached connection per database (None = no default database)."""
    conn = _CONNECTIONS.get(db)
    if conn is None or not conn.open:
        conn = _CONNECTIONS[db] = connect(db)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def close_all() -> None:
    for conn in _CONNECTIONS.values():
        if conn.open:
            conn.close()
    _CONNECTIONS.clear()


def docker(container: str, argv: List[str], stdin=None, stdout=None) -> None:
    env = ["-e", f"MYSQL_PWD={os.environ.get('MYSQL_PASSWORD', '')}"]  # keeps the password off the argv
    cmd = ["docker", "exec", "-i", *env, container, *argv]
    proc = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, check=False)  # nosec B603
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-500:]}")


def tables(db: str) -> List[str]:
    rows = query(None, "SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                       "AND table_type='BASE TABLE' ORDER BY table_name", (db,))
    return [r[0] for r in rows]


def q(name: str) -> str:
    """Backtick-quote an identifier read from information_schema."""
    return "`" + name.replace("`", "``") + "`"


# ── Steps ───────────────────────────────────────────────────────────────────

def dump(drill: Drill, path: Path) -> str:
    a = drill.args
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        docker(a.container, ["mysqldump", f"-u{os.environ.get('MYSQL_USER', 'root')}",
                             "--single-transaction", "--quick", "--routines", "--triggers", "--events",
                             "--hex-blob", "--no-tablespaces", "--set-gtid-purged=OFF", a.source], stdout=out)
    return f"{path.stat().st_size / 1e6:.1f} MB -> {path}"


def recreate(drill: Drill) -> str:
    a = drill.args
    charset, collation = query(None, "SELECT default_character_set_name, default_collation_name "
                                     "FROM information_schema.schemata WHERE schema_name=%s", (a.source,))[0]
    stale = _CONNECTIONS.pop(a.target, None)
    if stale is not None and stale.open:
        stale.close()
    query(None, f"DROP DATABASE IF EXISTS {q(a.target)}")
    query(None, f"CREATE DATABASE {q(a.target)} CHARACTER SET {charset} COLLATE {collation}")
    return f"{a.target} ({charset}/{collation})"


def restore(drill: Drill, path: Path) -> str:
    a = drill.args
    user = os.environ.get("MYSQL_USER", "root")
    with path.open("rb") as src:
        docker(a.container, ["mysql", f"-u{user}", a.target], stdin=src)
    return f"{len(tables(a.target))} tables"


def alembic_current(drill: Drill) -> str:
    a = drill.args
    env = {**os.environ, "MYSQL_DATABASE": a.target, "OPENAI_API_KEY": "", "RUN_BACKGROUND_JOBS": "false"}
    env.pop("DATABASE_URL", None)
    env.pop("Database_URL", None)
    proc = subprocess.run([a.python, "-m", "alembic", "current"], cwd=ROOT, env=env, capture_output=True,  # nosec B603
                          text=True, check=False)
    current = " ".join(re.findall(r"^([0-9a-z_]+)(?: \(head\))?\s*$", proc.stdout, re.M))
    source_rev = ",".join(r[0] for r in query(a.source, "SELECT version_num FROM alembic_version"))
    drill.check("alembic current on target == source alembic_version",
                proc.returncode == 0 and current.split() == source_rev.split(","),
                f"target={current or proc.stderr.strip()[-200:]} source={source_rev} head={'(head)' in proc.stdout}")
    return current


def row_counts(drill: Drill) -> Dict[str, Tuple[int, int]]:
    a = drill.args
    counts: Dict[str, Tuple[int, int]] = {}
    for t in tables(a.source):
        src = query(a.source, f"SELECT COUNT(*) FROM {q(t)}")[0][0]  # nosec B608 - identifier from information_schema
        dst = query(a.target, f"SELECT COUNT(*) FROM {q(t)}")[0][0]  # nosec B608
        counts[t] = (src, dst)
    bad = {t: c for t, c in counts.items() if c[0] != c[1]}
    drill.check("row counts equal for every table", not bad, f"{len(counts)} tables, {sum(c[1] for c in counts.values())} rows"
                + (f"; mismatches {bad}" if bad else ""))
    return counts


def content_checksum(db: str, table: str, columns: List[str]) -> Tuple[int, int]:
    """Order-independent checksum of every column value: (BIT_XOR, SUM) of CRC32 per row. Unlike CHECKSUM
    TABLE it ignores physical storage (JSON partial updates leave a different binary layout behind)."""
    row = "CONCAT_WS('|', " + ", ".join(f"COALESCE(CAST({q(c)} AS CHAR), '\\N')" for c in columns) + ")"
    xor, total = query(db, f"SELECT BIT_XOR(CRC32({row})), SUM(CRC32({row})) FROM {q(table)}")[0]  # nosec B608
    return int(xor or 0), int(total or 0)


def checksums(drill: Drill) -> str:
    a = drill.args
    names = tables(a.source)
    bad = []
    for t in names:
        cols = [r[0] for r in query(None, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                                          "AND table_name=%s ORDER BY ordinal_position", (a.source, t))]
        if content_checksum(a.source, t, cols) != content_checksum(a.target, t, cols):
            bad.append(t)
    drill.check("content checksum equal for every table", not bad, f"differs: {bad}" if bad else f"{len(names)} tables")
    return f"{len(names) - len(bad)}/{len(names)} equal"


def fk_orphans(drill: Drill) -> str:
    db = drill.args.target
    fks = query(None, "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
                      "FROM information_schema.key_column_usage WHERE table_schema=%s "
                      "AND referenced_table_name IS NOT NULL", (db,))
    orphans = {}
    for table, col, ref_table, ref_col in fks:
        n = query(db, f"SELECT COUNT(*) FROM {q(table)} c WHERE c.{q(col)} IS NOT NULL AND NOT EXISTS "  # nosec B608
                      f"(SELECT 1 FROM {q(ref_table)} p WHERE p.{q(ref_col)} = c.{q(col)})")[0][0]
        if n:
            orphans[f"{table}.{col}->{ref_table}"] = n
    drill.check("no foreign-key orphans", not orphans, f"{len(fks)} FKs checked" + (f"; orphans {orphans}" if orphans else ""))
    return f"{len(fks)} FKs"


def unique_keys(drill: Drill) -> str:
    db = drill.args.target
    rows = query(None, "SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) "
                       "FROM information_schema.statistics WHERE table_schema=%s AND non_unique=0 "
                       "AND column_name IS NOT NULL GROUP BY table_name, index_name", (db,))
    dupes = {}
    for table, index, cols in rows:
        columns = [q(c) for c in cols.split(",")]
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in columns)
        n = query(db, f"SELECT COUNT(*) FROM (SELECT 1 FROM {q(table)} WHERE {not_null} "  # nosec B608
                      f"GROUP BY {', '.join(columns)} HAVING COUNT(*) > 1) d")[0][0]
        if n:
            dupes[f"{table}.{index}"] = n
    drill.check("no duplicates in unique indexes", not dupes, f"{len(rows)} unique indexes" + (f"; dupes {dupes}" if dupes else ""))
    return f"{len(rows)} unique indexes"


SCHEMA_QUERIES = {
    "tables": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s",
    "indexes": "SELECT COUNT(DISTINCT table_name, index_name) FROM information_schema.statistics WHERE table_schema=%s",
    "foreign keys": "SELECT COUNT(*) FROM information_schema.referential_constraints WHERE constraint_schema=%s",
    "generated columns": "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=%s "
                         "AND generation_expression <> ''",
    "check constraints": "SELECT COUNT(*) FROM information_schema.check_constraints WHERE constraint_schema=%s",
    "triggers": "SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=%s",
}


def schema_parity(drill: Drill) -> str:
    a = drill.args
    diffs = {}
    for label, sql in SCHEMA_QUERIES.items():
        src, dst = query(None, sql, (a.source,))[0][0], query(None, sql, (a.target,))[0][0]
        if src != dst:
            diffs[label] = (src, dst)
    drill.check("schema objects match (tables, indexes, FKs, generated columns, checks, triggers)", not diffs,
                f"diffs {diffs}" if diffs else "")
    return "match" if not diffs else "differs"


# ── Report ──────────────────────────────────────────────────────────────────

def markdown(drill: Drill, counts: Dict[str, Tuple[int, int]]) -> str:
    a = drill.args
    lines = ["# Restore drill", "", f"- source: `{a.source}`, target: `{a.target}`, container: `{a.container}`",
             f"- run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
             "| step | seconds | detail |", "|---|---:|---|"]
    lines += [f"| {s['step']} | {s['seconds']} | {s['detail']} |" for s in drill.steps]
    lines += ["", f"**Total: {sum(s['seconds'] for s in drill.steps):.1f} s**", "", "| check | result | detail |", "|---|---|---|"]
    lines += [f"| {c['check']} | {'PASS' if c['ok'] else 'FAIL'} | {c['detail']} |" for c in drill.checks]
    big = sorted(counts.items(), key=lambda kv: -kv[1][0])[:15]
    lines += ["", "Largest tables (source / restored rows):", "", "| table | source | restored |", "|---|---:|---:|"]
    lines += [f"| {t} | {s} | {d} |" for t, (s, d) in big]
    lines += ["", f"**Overall: {'PASS' if all(c['ok'] for c in drill.checks) else 'FAIL'}**", ""]
    return "\n".join(lines)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default=os.environ.get("MYSQL_DATABASE") or "vtest_load")
    p.add_argument("--target", default=None, help="default <source>_restore")
    p.add_argument("--container", default="vector-mysql-1")
    p.add_argument("--dump-dir", default=str(ROOT / "reports" / "load" / "drill"))
    p.add_argument("--python", default=sys.executable, help="interpreter with alembic + the app installed")
    p.add_argument("--keep", action="store_true", help="keep the restored database and the dump file")
    a = p.parse_args(argv)
    a.target = a.target or f"{a.source}_restore"
    return a


def run(drill: Drill) -> Dict[str, Tuple[int, int]]:
    a = drill.args
    dump_path = Path(a.dump_dir) / f"{a.source}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sql"
    drill.step("1 mysqldump source", lambda: dump(drill, dump_path))
    drill.step("2 drop + recreate target", lambda: recreate(drill))
    drill.step("3 restore dump into target", lambda: restore(drill, dump_path))
    drill.step("4 alembic current (target)", lambda: alembic_current(drill))
    counts = drill.step("5 row counts per table", lambda: row_counts(drill))
    drill.step("6 content checksum per table", lambda: checksums(drill))
    drill.step("7a FK orphan check", lambda: fk_orphans(drill))
    drill.step("7b unique-key check", lambda: unique_keys(drill))
    drill.step("7c schema parity", lambda: schema_parity(drill))
    if not a.keep:
        close_all()  # the cached connection to the target would otherwise hold it open
        query(None, f"DROP DATABASE IF EXISTS {q(a.target)}")
        dump_path.unlink(missing_ok=True)
        print(f"dropped {a.target} and removed the dump (use --keep to keep them)")
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)
    reason = check_names(a.source, a.target, os.environ.get("MYSQL_HOST", "127.0.0.1"))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    if not query(None, "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (a.source,)):
        print(f"source database {a.source} does not exist", file=sys.stderr)
        return 2
    drill = Drill(a)
    try:
        counts = run(drill)
    except (RuntimeError, pymysql.MySQLError, OSError) as e:
        drill.check("drill completed", False, str(e)[:300])
        counts = {}
    md = markdown(drill, counts)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "reports" / "load" / f"restore_drill_{stamp}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps({"source": a.source, "target": a.target, "steps": drill.steps,
                                                    "checks": drill.checks, "row_counts": counts}, indent=2))
    out.with_suffix(".md").write_text(md)
    print("\n" + md)
    print(f"Results: {out.with_suffix('.json')} and {out.with_suffix('.md')}")
    return 0 if drill.checks and all(c["ok"] for c in drill.checks) else 1


if __name__ == "__main__":
    sys.exit(main())
