"""
conftest.py — patch pkg_resources and heavy OTel instrumentation before
app.py is imported so the test suite runs without a live collector or
a fully-compatible OTel SDK version.

Also resolves the Postgres hostname: the app defaults to host="postgres"
(Docker service name) which only resolves inside the Docker network.
When running tests on the host we override POSTGRES_HOST to "localhost"
so the already-published port 5432 is used instead.
"""
import os
import sys
import types
import unittest.mock as mock

# ---------------------------------------------------------------------------
# Resolve Postgres host for tests running outside Docker
# ---------------------------------------------------------------------------
def _resolve_postgres_host() -> str:
    """Return a reachable Postgres host, preferring the env var but falling
    back to localhost when the configured hostname can't be resolved."""
    import socket
    configured = os.getenv("POSTGRES_HOST", "postgres")
    try:
        socket.getaddrinfo(configured, 5432)
        return configured
    except socket.gaierror:
        return "localhost"

os.environ.setdefault("POSTGRES_HOST", _resolve_postgres_host())

# ---------------------------------------------------------------------------
# Ensure audit_logs table exists before any test that hits Postgres.
# On a fresh volume the app creates the table on startup, but the test
# process connects directly and may run before the app container has started.
# ---------------------------------------------------------------------------
def _ensure_audit_table() -> None:
    try:
        import psycopg2
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = int(os.getenv("POSTGRES_PORT", 5432))
        dbname = os.getenv("POSTGRES_DB", "observability")
        user = os.getenv("POSTGRES_USER", "observability")
        password = os.getenv("POSTGRES_PASSWORD", "observability")
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password, connect_timeout=2,
        )
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    endpoint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_time_seconds DOUBLE PRECISION,
                    process_memory_rss BIGINT,
                    process_cpu_seconds DOUBLE PRECISION,
                    system_loadavg_1m DOUBLE PRECISION,
                    container_memory_current BIGINT,
                    container_memory_limit BIGINT,
                    container_memory_percent DOUBLE PRECISION,
                    container_cpu_usage_ns BIGINT,
                    details JSONB
                );
            """)
        conn.commit()
        conn.close()
    except Exception:
        pass  # Postgres not available — DB tests will be skipped anyway

_ensure_audit_table()

# ---------------------------------------------------------------------------
# Silence OTel SDK export retry stderr noise during tests.
# When running on the host there is no collector on localhost:4317, so the
# SDK logs "Transient error ... retrying" to stderr on every test that
# imports app.py.  Setting the exporter timeout to 1 s and suppressing the
# internal OTel logger keeps the test output clean.
# ---------------------------------------------------------------------------
os.environ.setdefault("OTEL_EXPORTER_OTLP_TIMEOUT", "1")

import logging
logging.getLogger("opentelemetry.exporter.otlp").setLevel(logging.CRITICAL)
logging.getLogger("opentelemetry.sdk").setLevel(logging.CRITICAL)

# ---------------------------------------------------------------------------
# Stub pkg_resources (removed in Python 3.13, still used by older OTel pkgs)
# ---------------------------------------------------------------------------
if "pkg_resources" not in sys.modules:
    pkg = types.ModuleType("pkg_resources")
    pkg.require = lambda *a, **kw: None

    class _Dist:
        def __init__(self, project_name="", version="0.0.0"):
            self.project_name = project_name
            self.version = version

    pkg.get_distribution = lambda name: _Dist(name)
    pkg.DistributionNotFound = Exception
    pkg.VersionConflict = Exception
    sys.modules["pkg_resources"] = pkg

# ---------------------------------------------------------------------------
# Stub FlaskInstrumentor so it's a no-op during tests
# ---------------------------------------------------------------------------
flask_instr_mod = types.ModuleType("opentelemetry.instrumentation.flask")

class _NoopFlaskInstrumentor:
    def instrument_app(self, app, **kwargs):
        pass
    def instrument(self, **kwargs):
        pass
    def uninstrument(self, **kwargs):
        pass

flask_instr_mod.FlaskInstrumentor = _NoopFlaskInstrumentor
sys.modules["opentelemetry.instrumentation.flask"] = flask_instr_mod
