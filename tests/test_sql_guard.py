"""The SQL editor guard rejects anything that is not a single read of the app tables."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "canary"))

from sql_guard import SqlGuardError, guard_readonly_sql


def _ok(sql: str) -> None:
    guard_readonly_sql(sql)


def _bad(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        guard_readonly_sql(sql)


def test_select_from_audit_logs():
    _ok("SELECT id, created_at, endpoint, status_code FROM audit_logs ORDER BY id DESC LIMIT 5;")


def test_cast_and_json_operators():
    _ok(
        "SELECT endpoint, status_code, count(*) AS total_calls, "
        "round(avg(response_time_seconds)::numeric, 4) AS avg_duration_sec "
        "FROM audit_logs GROUP BY endpoint, status_code ORDER BY total_calls DESC;"
    )


def test_string_may_mention_drop():
    _ok("SELECT id FROM audit_logs WHERE endpoint = 'drop table audit_logs' LIMIT 1;")


def test_public_schema_and_cte():
    _ok(
        "WITH recent AS (SELECT id FROM public.audit_logs) "
        "SELECT id FROM recent ORDER BY id DESC LIMIT 5;"
    )


def test_join_allowed_tables():
    _ok(
        "SELECT a.id, s.name FROM audit_logs a "
        "LEFT JOIN sql_saved_queries s ON s.created_by = 'demouser' LIMIT 5;"
    )


def test_editor_audit_preview():
    _ok(
        "SELECT id, created_at, username, status, row_count, "
        "left(query_text, 120) AS query_preview "
        "FROM sql_query_audit ORDER BY id DESC LIMIT 50;"
    )


def test_empty_and_non_select():
    _bad("")
    _bad("DROP TABLE audit_logs")
    _bad("INSERT INTO audit_logs (endpoint, status_code) VALUES ('/x', 200)")
    _bad("UPDATE audit_logs SET status_code = 500")
    _bad("DELETE FROM audit_logs")


def test_stacked_statements_and_catalogs():
    _bad("SELECT id FROM audit_logs; DROP TABLE audit_logs")
    _bad("SELECT * FROM pg_stat_activity")
    _bad("SELECT table_name FROM information_schema.tables")
    _bad("SELECT id FROM audit_logs UNION SELECT usename FROM pg_user")
    _bad("SELECT pg_read_file('/etc/passwd') FROM audit_logs")
    _bad("SELECT 1")


def test_comment_cannot_hide_a_second_statement():
    _ok("SELECT id FROM audit_logs /* drop table audit_logs */ LIMIT 1;")
    _bad("SELECT id FROM audit_logs -- comment\n; DROP TABLE audit_logs")
