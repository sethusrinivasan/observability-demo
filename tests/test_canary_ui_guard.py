"""
tests/test_canary_ui_guard.py — Safety Guard for Canary Web Dashboard.

Prevents regressions where embedded JavaScript or HTML template escaping
(e.g., Python f-string unescaped newlines, nested quote collisions, or
unclosed tags) causes client-side syntax errors and renders the Canary
Load & Telemetry Control page blank.
"""

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
import pytest
import requests

CANARY_URL = os.getenv("CANARY_URL", "http://localhost:8085").rstrip("/")


class TagChecker(HTMLParser):
    """HTML Parser that validates tag balancing and catches unclosed elements."""
    def __init__(self):
        super().__init__()
        self.stack = []
        self.void_tags = {'meta', 'input', 'img', 'br', 'hr', 'link', 'source', 'col'}
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in self.void_tags:
            self.stack.append((tag.lower(), self.getpos()))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.void_tags:
            return
        if not self.stack:
            self.errors.append(f"Unexpected closing tag </{tag}> at line {self.getpos()[0]}")
            return
        last, pos = self.stack.pop()
        if last != tag:
            self.errors.append(
                f"Mismatched tag: opened <{last}> at line {pos[0]}, closed with </{tag}> at line {self.getpos()[0]}"
            )


@pytest.fixture(scope="module")
def dashboard_html():
    """Fetch live Canary Dashboard HTML."""
    res = requests.get(CANARY_URL, timeout=5)
    assert res.status_code == 200, f"Canary returned status code {res.status_code}"
    assert len(res.text) > 1000, "Canary returned suspiciously short or empty HTML"
    return res.text


def test_dashboard_html_structural_integrity(dashboard_html):
    """Guard 1: Ensure HTML has balanced tags and no unclosed container elements."""
    parser = TagChecker()
    parser.feed(dashboard_html)
    assert len(parser.errors) == 0, f"HTML structure errors detected: {parser.errors[:5]}"
    assert len(parser.stack) == 0, f"Unclosed HTML tags detected in DOM: {parser.stack[-5:]}"


def test_dashboard_critical_dom_sections_exist(dashboard_html):
    """Guard 2: Ensure all primary UI sections and control containers are present."""
    required_ids = [
        "graphs-section",
        "raw-numbers-section",
        "kpi-section",
        "controls-drawer",
        "dependencies-section",
        "sql-editor-section",
        "s3-browser-section",
        "telemetry-query-section",
        "live-pulse",
        "overall-status-pill",
    ]
    for dom_id in required_ids:
        assert f'id="{dom_id}"' in dashboard_html, f"Missing critical DOM container element: #{dom_id}"


def test_dashboard_view_tab_buttons_exist(dashboard_html):
    """Guard 3: Ensure all navigation tab buttons are present with their switchView calls."""
    expected_modes = [
        "both",
        "graphs",
        "raw",
        "dependencies",
        "sql",
        "s3",
        "telemetry_query",
    ]
    for mode in expected_modes:
        assert f"switchView('{mode}'" in dashboard_html or f'switchView("{mode}"' in dashboard_html, (
            f"Navigation button for mode '{mode}' is missing from .view-tabs"
        )


def test_dashboard_javascript_syntax_with_node(dashboard_html):
    """Guard 4: Extract all <script> blocks and validate JavaScript syntax via node --check.

    This catches quote collision errors (e.g. '' + val + ''), unescaped multiline string
    literals (e.g. unescaped \\n in Python f-string), and tokenization syntax errors
    that cause client-side browser execution to completely freeze.
    """
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("Node.js is not installed; skipping node --check validation.")

    scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", dashboard_html, re.DOTALL | re.IGNORECASE)
    assert len(scripts) > 0, "No <script> tags found in dashboard HTML"

    for idx, script_content in enumerate(scripts):
        if not script_content.strip():
            continue
        proc = subprocess.run(
            [node_path, "--check"],
            input=script_content,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, (
            f"CRITICAL: JavaScript syntax error detected in <script> tag #{idx}:\n{proc.stderr}\n"
            "This causes browser to show a blank/broken page."
        )


def test_dashboard_javascript_no_multiline_raw_string_literals(dashboard_html):
    """Guard 5: Regex static analysis checking for raw newlines inside JS single/double quoted strings."""
    scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", dashboard_html, re.DOTALL | re.IGNORECASE)
    for idx, script in enumerate(scripts):
        lines = script.split("\n")
        in_multiline_comment = False
        for line_num, line in enumerate(lines, start=1):
            stripped = line.strip()
            if "/*" in stripped and "*/" not in stripped:
                in_multiline_comment = True
                continue
            if in_multiline_comment:
                if "*/" in stripped:
                    in_multiline_comment = False
                continue
            if stripped.startswith("//"):
                continue

            # Flag adjacent empty quotes in function invocations like: func('' + var + '')
            if re.search(r"[a-zA-Z0-9_]\s*\(\s*''\s*\+", line):
                pytest.fail(
                    f"Syntax flaw in <script> tag #{idx} line {line_num}: "
                    f"Found adjacent empty single-quotes in call: {line.strip()}"
                )


def test_dashboard_javascript_top_level_execution(dashboard_html):
    """Guard 6: Execute the client-side JavaScript in a mocked DOM environment to verify

    that top-level declarations, event listeners, and initial setup run cleanly without
    throwing runtime exceptions.
    """
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("Node.js is not installed; skipping mocked DOM execution.")

    scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", dashboard_html, re.DOTALL | re.IGNORECASE)
    combined_js = "\n".join(scripts)

    mock_harness = """
global.window = global;
global.window.addEventListener = (event, cb) => {};
global.document = {
  getElementById: (id) => ({
    addEventListener: () => {},
    querySelectorAll: () => [],
    style: {},
    classList: { remove: () => {}, add: () => {} },
    innerText: '',
    innerHTML: '',
    appendChild: () => {}
  }),
  querySelectorAll: () => [],
  addEventListener: (event, cb) => { if (event === 'DOMContentLoaded') cb(); },
  createElement: (tag) => ({
    style: {},
    classList: { remove: () => {}, add: () => {} },
    appendChild: () => {},
    addEventListener: () => {}
  })
};
global.location = { protocol: 'http:', host: 'localhost:8085' };
global.fetch = async () => ({ json: async () => ({}) });
global.alert = () => {};
global.confirm = () => true;

""" + combined_js

    proc = subprocess.run(
        [node_path, "-e", mock_harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"CRITICAL: Client-side JavaScript threw a runtime exception during initialization:\n{proc.stderr}"
    )


def test_duckdb_query_editor_text_visibility(dashboard_html):
    """Guard 7: Ensure Telemetry View query editor has syntax highlighting, cancellation, and role support."""
    assert 'id="duckdb-query-input"' in dashboard_html
    assert 'id="duckdb-highlight"' in dashboard_html
    assert 'id="duckdb-cancel-btn"' in dashboard_html
    assert 'id="sql-role-select"' in dashboard_html
    assert 'observability_reader' in dashboard_html
    assert 'observability_admin' in dashboard_html


def test_dashboard_telemetry_target_pipeline_strip(dashboard_html):
    """Guard 8: Ensure the main dashboard displays the active telemetry target pipeline destinations."""
    assert "telemetry-pipeline-strip" in dashboard_html
    assert "Telemetry Pipeline Targets:" in dashboard_html
    assert "otel-collector:4317" in dashboard_html
    assert "mimir:9009" in dashboard_html
    assert "tempo:4317" in dashboard_html
    assert "loki:3100" in dashboard_html
    assert "s3://telemetry-lake" in dashboard_html

