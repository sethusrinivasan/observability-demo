"""
canary/sql_ast.py
AST-based SQL Query Fingerprinting and pg_stat_statements correlation engine.

Parses arbitrary SQL into an Abstract Syntax Tree (AST), strips literal parameters
(numbers, strings, binds, lists) to produce canonical query skeletons, derives
unique deterministic fingerprint IDs (fp_XXXXXXXXXXXX), maintains a persistent master
fingerprint registry in PostgreSQL (query_fingerprint_registry) and in-memory,
and correlates pg_stat_statements metrics into unified, deduplicated query patterns.
Supports 30-minute automated background scheduling and on-demand trigger.
"""

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("canary.sql_ast")

# In-memory master fingerprint store
MASTER_QUERY_FINGERPRINTS: dict[str, dict] = {}
_REGISTRY_LOCK = threading.Lock()

# Scheduler state
SCHEDULER_STATE = {
    "enabled": True,
    "interval_seconds": 1800,  # 30 minutes
    "interval_minutes": 30,
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_error": None,
    "total_fingerprints": 0,
}
_SCHEDULER_THREAD = None
_SCHEDULER_TRIGGER_EVENT = threading.Event()
_SCHEDULER_STOP_EVENT = threading.Event()


def strip_sql_parameters(sql: str) -> str:
    """Strip all parameters and literal constants from SQL query to form canonical skeleton."""
    if not sql:
        return ""
    s = sql.strip().rstrip(";")
    # 1. Strip comments
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"/\*[\s\S]*?\*/", " ", s)

    # 2. String literals: $$...$$, E'...', '...'
    s = re.sub(r"\$\$[\s\S]*?\$\$", " ? ", s)
    s = re.sub(r"E?'(?:''|\\.|[^'])*'", " ? ", s)

    # 3. Dollar / colon bind variables: $1, :arg
    s = re.sub(r"\$[0-9]+", " ? ", s)
    s = re.sub(r":[a-zA-Z_][a-zA-Z0-9_]*", " ? ", s)

    # 4. Hex & UUID literals
    s = re.sub(r"\b0x[0-9a-fA-F]+\b", " ? ", s)
    s = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", " ? ", s)

    # 5. Standalone numbers (integers, decimals, negative, exponential)
    s = re.sub(r"(?<![a-zA-Z0-9_])[-+]?(?:\d+\.\d+|\d+\.|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![a-zA-Z0-9_])", " ? ", s)

    # 6. Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # 7. Collapse repeated parameter lists: IN (?, ?, ...) -> IN (?)
    s = re.sub(r"\bIN\s*\(\s*(?:\?\s*,\s*)*\?\s*\)", "IN (?)", s, flags=re.IGNORECASE)

    # 8. Collapse VALUES (...), (...) -> VALUES (?)
    s = re.sub(r"\bVALUES\s*\([^\)]*\)(?:\s*,\s*\([^\)]*\))*", "VALUES (?)", s, flags=re.IGNORECASE)

    # Final cleanup
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_sql_to_ast(sql: str) -> dict:
    """Parse a SQL query into an Abstract Syntax Tree (AST) dictionary."""
    stripped = strip_sql_parameters(sql)
    if not stripped:
        return {
            "type": "EmptyStatement",
            "statement_type": "EMPTY",
            "canonical_skeleton": "",
            "fingerprint_id": "fp_000000000000",
            "primary_table": None,
            "targets": [],
            "tables": [],
            "joins": [],
            "where": None,
        }

    tokens = stripped.split()
    first_token = tokens[0].upper() if tokens else "UNKNOWN"

    ast = {
        "statement_type": first_token,
        "raw_skeleton": stripped,
        "primary_table": None,
        "clauses": {},
    }

    if first_token == "SELECT":
        ast.update(_parse_select_ast(stripped))
    elif first_token == "INSERT":
        ast.update(_parse_insert_ast(stripped))
    elif first_token == "UPDATE":
        ast.update(_parse_update_ast(stripped))
    elif first_token == "DELETE":
        ast.update(_parse_delete_ast(stripped))
    else:
        ast["type"] = "GenericStatement"
        ast["projections"] = []
        ast["tables"] = []

    # Reconstruct canonical skeleton SQL from AST
    canonical = _reconstruct_canonical_skeleton(ast)
    ast["canonical_skeleton"] = canonical
    ast["fingerprint_id"] = "fp_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return ast


def normalize_sql_to_ast(sql: str) -> dict:
    """Alias for parse_sql_to_ast."""
    return parse_sql_to_ast(sql)


def compute_query_fingerprint(sql: str) -> tuple[str, str, dict]:
    """Return (fingerprint_id, canonical_skeleton, ast_dict) for given query."""
    ast = parse_sql_to_ast(sql)
    return ast["fingerprint_id"], ast["canonical_skeleton"], ast


def _parse_select_ast(sql: str) -> dict:
    clause_keywords = ["FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET"]
    upper_sql = sql.upper()

    positions = {}
    for kw in clause_keywords:
        m = re.search(r"\b" + kw.replace(" ", r"\s+") + r"\b", upper_sql)
        if m:
            positions[kw] = (m.start(), m.end())

    sorted_kws = sorted(positions.keys(), key=lambda k: positions[k][0])

    sel_match = re.search(r"\bSELECT\b", upper_sql)
    sel_end = sel_match.end() if sel_match else 6
    first_clause_pos = positions[sorted_kws[0]][0] if sorted_kws else len(sql)
    targets_str = sql[sel_end:first_clause_pos].strip()
    targets = [t.strip() for t in targets_str.split(",") if t.strip()]

    clauses_content = {}
    for i, kw in enumerate(sorted_kws):
        start = positions[kw][1]
        end = positions[sorted_kws[i + 1]][0] if i + 1 < len(sorted_kws) else len(sql)
        clauses_content[kw] = sql[start:end].strip()

    from_raw = clauses_content.get("FROM", "")
    tables = []
    joins = []
    if from_raw:
        join_pattern = re.compile(
            r"\b(LEFT\s+OUTER\s+JOIN|RIGHT\s+OUTER\s+JOIN|FULL\s+OUTER\s+JOIN|INNER\s+JOIN|CROSS\s+JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|JOIN)\b",
            re.IGNORECASE,
        )
        join_matches = list(join_pattern.finditer(from_raw))
        if not join_matches:
            for tbl in from_raw.split(","):
                tbl_parts = tbl.strip().split()
                if tbl_parts:
                    tables.append({"table": tbl_parts[0], "alias": tbl_parts[1] if len(tbl_parts) > 1 else None})
        else:
            first_tbl_str = from_raw[:join_matches[0].start()].strip()
            tbl_parts = first_tbl_str.split()
            if tbl_parts:
                tables.append({"table": tbl_parts[0], "alias": tbl_parts[1] if len(tbl_parts) > 1 else None})
            for j_idx, j_m in enumerate(join_matches):
                j_type = re.sub(r"\s+", " ", j_m.group().upper())
                j_start = j_m.end()
                j_end = join_matches[j_idx + 1].start() if j_idx + 1 < len(join_matches) else len(from_raw)
                j_body = from_raw[j_start:j_end].strip()
                on_m = re.search(r"\bON\b", j_body, re.IGNORECASE)
                if on_m:
                    tbl_part = j_body[:on_m.start()].strip()
                    on_part = j_body[on_m.end():].strip()
                else:
                    tbl_part = j_body
                    on_part = None
                p = tbl_part.split()
                joins.append({
                    "type": j_type,
                    "table": p[0] if p else "",
                    "alias": p[1] if len(p) > 1 else None,
                    "on": on_part,
                })

    where_raw = clauses_content.get("WHERE", "")
    where_tree = None
    if where_raw:
        conds = re.split(r"\bAND\b|\bOR\b", where_raw, flags=re.IGNORECASE)
        where_tree = {
            "type": "WhereExpression",
            "conditions": [c.strip() for c in conds if c.strip()],
            "raw": where_raw,
        }

    group_by = [c.strip() for c in clauses_content.get("GROUP BY", "").split(",") if c.strip()]
    order_by = [c.strip() for c in clauses_content.get("ORDER BY", "").split(",") if c.strip()]
    limit = clauses_content.get("LIMIT", "").strip() or None
    offset = clauses_content.get("OFFSET", "").strip() or None

    primary_table = tables[0]["table"] if tables else None

    return {
        "type": "SelectStatement",
        "primary_table": primary_table,
        "targets": targets,
        "tables": tables,
        "joins": joins,
        "where": where_tree,
        "group_by": group_by,
        "order_by": order_by,
        "limit": limit,
        "offset": offset,
    }


def _parse_insert_ast(sql: str) -> dict:
    m = re.search(r"\bINSERT\s+INTO\s+([a-zA-Z0-9_\.\"]+)(?:\s*\(([^\)]+)\))?", sql, re.IGNORECASE)
    tbl = m.group(1) if m else "unknown"
    cols = [c.strip() for c in m.group(2).split(",")] if m and m.group(2) else []
    return {
        "type": "InsertStatement",
        "primary_table": tbl,
        "columns": cols,
        "values": "(?)",
    }


def _parse_update_ast(sql: str) -> dict:
    m = re.search(r"\bUPDATE\s+([a-zA-Z0-9_\.\"]+)\s+SET\s+(.*?)(?:\s+WHERE\s+(.*))?$", sql, re.IGNORECASE)
    tbl = m.group(1) if m else "unknown"
    set_clause = m.group(2).strip() if m and m.group(2) else ""
    where_clause = m.group(3).strip() if m and m.group(3) else None
    return {
        "type": "UpdateStatement",
        "primary_table": tbl,
        "set_assignments": [s.strip() for s in set_clause.split(",") if s.strip()],
        "where": {"raw": where_clause} if where_clause else None,
    }


def _parse_delete_ast(sql: str) -> dict:
    m = re.search(r"\bDELETE\s+FROM\s+([a-zA-Z0-9_\.\"]+)(?:\s+WHERE\s+(.*))?$", sql, re.IGNORECASE)
    tbl = m.group(1) if m else "unknown"
    where_clause = m.group(2).strip() if m and m.group(2) else None
    return {
        "type": "DeleteStatement",
        "primary_table": tbl,
        "where": {"raw": where_clause} if where_clause else None,
    }


def _reconstruct_canonical_skeleton(ast: dict) -> str:
    stype = ast.get("statement_type", "SELECT")
    if stype == "SELECT":
        parts = ["SELECT"]
        targets = ast.get("targets") or ["?"]
        parts.append(", ".join(targets))
        tables = ast.get("tables", [])
        if tables:
            tbl_strs = []
            for t in tables:
                s = t["table"]
                if t.get("alias"):
                    s += f" {t['alias']}"
                tbl_strs.append(s)
            parts.append("FROM " + ", ".join(tbl_strs))
        joins = ast.get("joins", [])
        for j in joins:
            js = f"{j['type']} {j['table']}"
            if j.get("alias"):
                js += f" {j['alias']}"
            if j.get("on"):
                js += f" ON {j['on']}"
            parts.append(js)
        where = ast.get("where")
        if where and where.get("conditions"):
            parts.append("WHERE " + " AND ".join(where["conditions"]))
        elif where and where.get("raw"):
            parts.append("WHERE " + where["raw"])
        if ast.get("group_by"):
            parts.append("GROUP BY " + ", ".join(ast["group_by"]))
        if ast.get("order_by"):
            parts.append("ORDER BY " + ", ".join(ast["order_by"]))
        if ast.get("limit"):
            parts.append("LIMIT ?")
        if ast.get("offset"):
            parts.append("OFFSET ?")
        return " ".join(parts)
    elif stype == "INSERT":
        tbl = ast.get("primary_table", "")
        cols = f" ({', '.join(ast.get('columns', []))})" if ast.get("columns") else ""
        return f"INSERT INTO {tbl}{cols} VALUES (?)"
    elif stype == "UPDATE":
        tbl = ast.get("primary_table", "")
        sets = ", ".join(ast.get("set_assignments") or ["? = ?"])
        wh = f" WHERE {ast['where']['raw']}" if ast.get("where") else ""
        return f"UPDATE {tbl} SET {sets}{wh}"
    elif stype == "DELETE":
        tbl = ast.get("primary_table", "")
        wh = f" WHERE {ast['where']['raw']}" if ast.get("where") else ""
        return f"DELETE FROM {tbl}{wh}"
    return ast.get("raw_skeleton", "")


def ensure_fingerprint_table(conn) -> bool:
    """Ensure query_fingerprint_registry table exists in PostgreSQL."""
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS query_fingerprint_registry (
                fingerprint_id VARCHAR(64) PRIMARY KEY,
                skeleton_pattern TEXT NOT NULL,
                ast_tree JSONB,
                statement_type VARCHAR(32),
                primary_table VARCHAR(128),
                sample_query TEXT,
                calls BIGINT DEFAULT 0,
                total_exec_time_ms DOUBLE PRECISION DEFAULT 0.0,
                mean_exec_time_ms DOUBLE PRECISION DEFAULT 0.0,
                rows_returned BIGINT DEFAULT 0,
                shared_blks_read BIGINT DEFAULT 0,
                shared_blks_hit BIGINT DEFAULT 0,
                cache_hit_pct DOUBLE PRECISION DEFAULT 100.0,
                shared_blks_written BIGINT DEFAULT 0,
                temp_blks BIGINT DEFAULT 0,
                wal_bytes BIGINT DEFAULT 0,
                est_network_kb DOUBLE PRECISION DEFAULT 0.0,
                primary_bottleneck VARCHAR(64),
                compute_cost DOUBLE PRECISION DEFAULT 0.0,
                io_cost DOUBLE PRECISION DEFAULT 0.0,
                est_cost_usd DOUBLE PRECISION DEFAULT 0.0,
                first_seen_at TIMESTAMPTZ DEFAULT now(),
                last_seen_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        logger.debug("Could not ensure query_fingerprint_registry table: %s", exc)
        return False


def correlate_pg_stat_statements(conn_or_none, additional_raw_queries=None) -> list[dict]:
    """
    Correlates queries from pg_stat_statements (and snapshots/activity) with AST fingerprints.
    Merges multiple parameterized occurrences of identical AST skeletons into a unified master
    fingerprint entry.
    """
    raw_entries = []

    # 1. Fetch from pg_stat_statements if connection available
    if conn_or_none:
        try:
            cur = conn_or_none.cursor()
            cur.execute("""
                SELECT 
                    s.query,
                    COALESCE(r.rolname, 'unknown') AS db_user,
                    s.calls,
                    s.total_exec_time::numeric AS total_time_ms,
                    s.mean_exec_time::numeric AS mean_time_ms,
                    s.rows,
                    s.shared_blks_read,
                    s.shared_blks_hit,
                    round((100.0 * s.shared_blks_hit / nullif(s.shared_blks_hit + s.shared_blks_read, 0))::numeric, 1) AS cache_hit_pct,
                    s.shared_blks_written,
                    (s.temp_blks_read + s.temp_blks_written) AS temp_blks,
                    coalesce(s.wal_bytes, 0) AS wal_bytes
                FROM pg_stat_statements s
                LEFT JOIN pg_roles r ON s.userid = r.oid
                ORDER BY s.total_exec_time DESC
                LIMIT 150;
            """)
            for row in cur.fetchall():
                raw_entries.append({
                    "query": row[0],
                    "db_user": row[1],
                    "calls": int(row[2] or 0),
                    "total_time_ms": float(row[3] or 0.0),
                    "mean_time_ms": float(row[4] or 0.0),
                    "rows": int(row[5] or 0),
                    "blks_read": int(row[6] or 0),
                    "blks_hit": int(row[7] or 0),
                    "cache_hit_pct": float(row[8]) if row[8] is not None else 100.0,
                    "blks_written": int(row[9] or 0),
                    "temp_blks": int(row[10] or 0),
                    "wal_bytes": int(row[11] or 0),
                })
        except Exception as exc:
            if hasattr(conn_or_none, "rollback"):
                conn_or_none.rollback()
            logger.debug("Failed querying pg_stat_statements for fingerprint correlation: %s", exc)

    if additional_raw_queries:
        raw_entries.extend(additional_raw_queries)

    # 2. Correlate and merge by AST fingerprint ID
    aggregated: dict[str, dict] = {}
    for entry in raw_entries:
        raw_query = entry.get("query") or entry.get("query_text") or entry.get("query_preview") or ""
        if not raw_query or len(raw_query.strip()) < 3:
            continue

        ast = parse_sql_to_ast(raw_query)
        fp_id = ast["fingerprint_id"]

        calls = int(entry.get("calls", 1))
        tot_ms = float(entry.get("total_time_ms", 0.0))
        rows = int(entry.get("rows", 0))
        blks_read = int(entry.get("blks_read", 0))
        blks_hit = int(entry.get("blks_hit", 0))
        blks_written = int(entry.get("blks_written", 0))
        temp_blks = int(entry.get("temp_blks", 0))
        wal_bytes = int(entry.get("wal_bytes", 0))
        user = entry.get("db_user", "unknown")

        if fp_id not in aggregated:
            aggregated[fp_id] = {
                "fingerprint_id": fp_id,
                "skeleton_pattern": ast["canonical_skeleton"],
                "ast_tree": ast,
                "statement_type": ast.get("statement_type", "SELECT"),
                "primary_table": ast.get("primary_table"),
                "sample_query": raw_query[:250],
                "db_users": {user},
                "correlated_variants_count": 1,
                "calls": calls,
                "total_time_ms": tot_ms,
                "rows": rows,
                "blks_read": blks_read,
                "blks_hit": blks_hit,
                "blks_written": blks_written,
                "temp_blks": temp_blks,
                "wal_bytes": wal_bytes,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
        else:
            agg = aggregated[fp_id]
            agg["calls"] += calls
            agg["total_time_ms"] += tot_ms
            agg["rows"] += rows
            agg["blks_read"] += blks_read
            agg["blks_hit"] += blks_hit
            agg["blks_written"] += blks_written
            agg["temp_blks"] += temp_blks
            agg["wal_bytes"] += wal_bytes
            agg["db_users"].add(user)
            agg["correlated_variants_count"] += 1
            agg["last_seen"] = datetime.now(timezone.utc).isoformat()

    # 3. Finalize calculated rates, costs, and bottleneck classifications
    result_list = []
    for fp_id, item in aggregated.items():
        calls = item["calls"]
        tot_ms = item["total_time_ms"]
        mean_ms = round(tot_ms / max(1, calls), 2)
        item["mean_time_ms"] = mean_ms

        blks_hit = item["blks_hit"]
        blks_read = item["blks_read"]
        blks_written = item["blks_written"]
        temp_blks = item["temp_blks"]
        wal_bytes = item["wal_bytes"]
        rows = item["rows"]

        total_blks = blks_hit + blks_read
        item["cache_hit_pct"] = round((100.0 * blks_hit / total_blks), 1) if total_blks > 0 else 100.0

        # Cost attribution formulas
        compute_cost = (tot_ms / 3600000.0) * 0.0416
        io_cost = ((blks_read + blks_written) / 1000000.0) * 0.20
        temp_wal_mb = (temp_blks * 8192 + wal_bytes) / (1024.0 * 1024.0)
        storage_cost = (temp_wal_mb / 1024.0) * (0.115 / 720.0)
        est_network_kb = round((rows * 120.0) / 1024.0, 1)
        network_cost = (est_network_kb / (1024.0 * 1024.0)) * 0.09
        total_cost = compute_cost + io_cost + storage_cost + network_cost

        item["compute_cost"] = round(compute_cost, 6)
        item["io_cost"] = round(io_cost, 6)
        item["est_network_kb"] = est_network_kb
        item["total_cost"] = round(total_cost, 6)
        item["est_cost_usd"] = round(total_cost, 6)

        # Bottleneck detection
        if blks_read > 500 or item["cache_hit_pct"] < 85.0:
            item["bottleneck"] = "Disk Read I/O"
        elif temp_blks > 0:
            item["bottleneck"] = "Temp Spill (work_mem)"
        elif tot_ms > 1000.0:
            item["bottleneck"] = "CPU Execution"
        elif calls > 5000:
            item["bottleneck"] = "High Volume"
        else:
            item["bottleneck"] = "Optimized (Cache)"

        sorted_users = sorted(item["db_users"])
        item["db_users"] = sorted_users
        item["db_user"] = ", ".join(sorted_users)
        item["query_preview"] = item["skeleton_pattern"][:140]
        result_list.append(item)

    # Sort descending by total compute time
    result_list.sort(key=lambda x: x["total_time_ms"], reverse=True)

    # 4. Update in-memory master dictionary & Postgres table
    with _REGISTRY_LOCK:
        for it in result_list:
            MASTER_QUERY_FINGERPRINTS[it["fingerprint_id"]] = it
        SCHEDULER_STATE["total_fingerprints"] = len(MASTER_QUERY_FINGERPRINTS)
        SCHEDULER_STATE["last_run_at"] = datetime.now(timezone.utc).isoformat()
        SCHEDULER_STATE["next_run_at"] = datetime.fromtimestamp(
            time.time() + SCHEDULER_STATE["interval_seconds"], tz=timezone.utc
        ).isoformat()
        SCHEDULER_STATE["run_count"] += 1

    # Persist to Postgres if connection is available
    if conn_or_none:
        try:
            ensure_fingerprint_table(conn_or_none)
            cur = conn_or_none.cursor()
            for it in result_list:
                cur.execute("""
                    INSERT INTO query_fingerprint_registry (
                        fingerprint_id, skeleton_pattern, ast_tree, statement_type,
                        primary_table, sample_query, calls, total_exec_time_ms,
                        mean_exec_time_ms, rows_returned, shared_blks_read, shared_blks_hit,
                        cache_hit_pct, shared_blks_written, temp_blks, wal_bytes,
                        est_network_kb, primary_bottleneck, compute_cost, io_cost,
                        est_cost_usd, last_seen_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, now()
                    )
                    ON CONFLICT (fingerprint_id) DO UPDATE SET
                        calls = EXCLUDED.calls,
                        total_exec_time_ms = EXCLUDED.total_exec_time_ms,
                        mean_exec_time_ms = EXCLUDED.mean_exec_time_ms,
                        rows_returned = EXCLUDED.rows_returned,
                        shared_blks_read = EXCLUDED.shared_blks_read,
                        shared_blks_hit = EXCLUDED.shared_blks_hit,
                        cache_hit_pct = EXCLUDED.cache_hit_pct,
                        shared_blks_written = EXCLUDED.shared_blks_written,
                        temp_blks = EXCLUDED.temp_blks,
                        wal_bytes = EXCLUDED.wal_bytes,
                        est_network_kb = EXCLUDED.est_network_kb,
                        primary_bottleneck = EXCLUDED.primary_bottleneck,
                        compute_cost = EXCLUDED.compute_cost,
                        io_cost = EXCLUDED.io_cost,
                        est_cost_usd = EXCLUDED.est_cost_usd,
                        last_seen_at = now();
                """, (
                    it["fingerprint_id"],
                    it["skeleton_pattern"],
                    json.dumps(it["ast_tree"]),
                    it["statement_type"],
                    it["primary_table"],
                    it["sample_query"],
                    it["calls"],
                    it["total_time_ms"],
                    it["mean_time_ms"],
                    it["rows"],
                    it["blks_read"],
                    it["blks_hit"],
                    it["cache_hit_pct"],
                    it["blks_written"],
                    it["temp_blks"],
                    it["wal_bytes"],
                    it["est_network_kb"],
                    it["bottleneck"],
                    it["compute_cost"],
                    it["io_cost"],
                    it["est_cost_usd"],
                ))
            conn_or_none.commit()
        except Exception as exc:
            if hasattr(conn_or_none, "rollback"):
                conn_or_none.rollback()
            logger.debug("Failed saving fingerprints to query_fingerprint_registry: %s", exc)

    return result_list


def get_master_fingerprints() -> list[dict]:
    """Retrieve full master fingerprint list from in-memory registry."""
    with _REGISTRY_LOCK:
        items = list(MASTER_QUERY_FINGERPRINTS.values())
    items.sort(key=lambda x: x.get("total_time_ms", 0.0), reverse=True)
    return items


def get_fingerprint_scheduler_status() -> dict:
    """Return live scheduler timing, status, and telemetry."""
    with _REGISTRY_LOCK:
        status = dict(SCHEDULER_STATE)
    return status


def _fingerprint_scheduler_loop(get_conn_callable, interval_seconds: int = 1800):
    """Background daemon loop executing AST query fingerprinting every 30 minutes (1800s)."""
    logger.info("Starting AST query fingerprint scheduler daemon (interval: %ds / 30m)...", interval_seconds)
    while not _SCHEDULER_STOP_EVENT.is_set():
        try:
            conn = None
            if callable(get_conn_callable):
                try:
                    conn = get_conn_callable()
                except Exception as c_exc:
                    logger.debug("Scheduler could not obtain Postgres connection: %s", c_exc)
            correlate_pg_stat_statements(conn)
        except Exception as loop_exc:
            SCHEDULER_STATE["last_error"] = str(loop_exc)
            logger.exception("Error in AST query fingerprint background loop: %s", loop_exc)

        # Sleep in short intervals or wake on trigger event
        t_target = time.time() + interval_seconds
        while time.time() < t_target and not _SCHEDULER_STOP_EVENT.is_set():
            if _SCHEDULER_TRIGGER_EVENT.wait(timeout=1.0):
                _SCHEDULER_TRIGGER_EVENT.clear()
                break


def start_fingerprint_scheduler(get_conn_callable=None, interval_seconds: int = 1800):
    """Start the 30-minute automated background thread for AST query fingerprinting."""
    global _SCHEDULER_THREAD
    if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
        return _SCHEDULER_THREAD

    _SCHEDULER_STOP_EVENT.clear()
    _SCHEDULER_TRIGGER_EVENT.clear()
    SCHEDULER_STATE["interval_seconds"] = interval_seconds
    SCHEDULER_STATE["interval_minutes"] = max(1, interval_seconds // 60)

    _SCHEDULER_THREAD = threading.Thread(
        target=_fingerprint_scheduler_loop,
        args=(get_conn_callable, interval_seconds),
        daemon=True,
        name="canary-ast-fingerprint-scheduler",
    )
    _SCHEDULER_THREAD.start()
    return _SCHEDULER_THREAD


def trigger_fingerprint_analysis_now(get_conn_callable=None) -> list[dict]:
    """Manually trigger AST query fingerprinting analysis immediately."""
    conn = None
    if callable(get_conn_callable):
        try:
            conn = get_conn_callable()
        except Exception as c_exc:
            logger.debug("Trigger could not obtain Postgres connection: %s", c_exc)
    res = correlate_pg_stat_statements(conn)
    _SCHEDULER_TRIGGER_EVENT.set()
    return res
