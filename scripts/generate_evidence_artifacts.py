#!/usr/bin/env python3
import json
import os
import subprocess
import time
from PIL import Image, ImageDraw, ImageFont

EVIDENCE_DIR = "/home/home/github/observability-demo/docs/evidence/issue-52"
os.makedirs(EVIDENCE_DIR, exist_ok=True)

ENDPOINTS = {
    "c": "https://omtosemwg4qxxjqlnbidwl4mvm0wvqva.lambda-url.us-east-2.on.aws/version",
    "dotnet": "https://4o5yjl47yxdg5e5tsj727mchb40evddy.lambda-url.us-east-2.on.aws/version",
    "go": "https://h74bl44slegkr7c6h32hyygzui0djbvn.lambda-url.us-east-2.on.aws/version",
    "java": "https://c5nmwnntg56s7v6gr2cowlmqqm0bcovb.lambda-url.us-east-2.on.aws/version",
    "node": "https://xo3ktoqz6qz3dm5o64hvgm22mq0zkqat.lambda-url.us-east-2.on.aws/version",
    "php": "https://gvn63eisikfv2zvzf52bqngwku0zhtkv.lambda-url.us-east-2.on.aws/version",
    "python": "https://mxhrcxceaeluv6gu2cod4quwpm0kfydz.lambda-url.us-east-2.on.aws/version",
    "rust": "https://nulam6s4glkv2wafy6tcrjn2j40fyhim.lambda-url.us-east-2.on.aws/version",
    "swift": "https://6cq3xl2oie6huoo4mxrqxxfgqu0upybj.lambda-url.us-east-2.on.aws/version"
}

print("Fetching current live endpoint responses...")
audit_results = {}
for lang, url in ENDPOINTS.items():
    try:
        out = subprocess.check_output(["curl", "-s", "--max-time", "5", url]).decode("utf-8")
        try:
            data = json.loads(out)
        except Exception:
            data = {"raw": out}
        audit_results[lang] = {
            "url": url,
            "status": "online",
            "response": data
        }
    except Exception as e:
        audit_results[lang] = {
            "url": url,
            "status": "error",
            "error": str(e)
        }

with open(f"{EVIDENCE_DIR}/live-endpoints-audit.json", "w") as f:
    json.dump(audit_results, f, indent=2)

print("Saved live-endpoints-audit.json")

# Generate HTML visual for polyglot endpoint audit
html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0d1117; color: #c9d1d9; padding: 24px; }}
  h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 12px; margin-bottom: 16px; font-size: 24px; }}
  .badge-drift {{ background: #f85149; color: white; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
  .badge-fixed {{ background: #238636; color: white; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #30363d; padding: 10px 14px; text-align: left; font-size: 13px; }}
  th {{ background: #161b22; color: #8b949e; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }}
  tr:nth-child(even) {{ background: #161b22; }}
  code {{ font-family: monospace; color: #79c0ff; background: rgba(110,118,129,0.4); padding: 2px 6px; border-radius: 4px; }}
  pre {{ margin: 0; font-size: 12px; color: #7ee787; }}
</style>
</head>
<body>
<h1>Observability Demo — Polyglot Runtime Live Audit Snapshot</h1>
<p style="color: #8b949e; margin-bottom: 20px;">Snapshot captured at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} for GitHub Issue #52 Audit</p>
<table>
  <thead>
    <tr>
      <th>Service</th>
      <th>Endpoint URL</th>
      <th>Observed Language</th>
      <th>Process Name</th>
      <th>Linux Comm (/proc/self/comm)</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
"""

for lang, res in audit_results.items():
    resp = res.get("response", {})
    obs_lang = resp.get("language", "unknown")
    proc_name = resp.get("process_name", "N/A")
    comm = resp.get("linux_comm", "N/A")
    
    is_drift = (obs_lang != lang and lang in ["php", "c", "java", "node", "rust", "dotnet"]) or (obs_lang == "golang" and lang != "go")
    status_badge = f'<span class="badge-drift">GO-CONTAINER DRIFT</span>' if is_drift else f'<span class="badge-fixed">GENUINE RUNTIME</span>'
    
    html_content += f"""
    <tr>
      <td><strong>{lang.upper()}</strong></td>
      <td><code>{res['url']}</code></td>
      <td style="color: {'#f85149' if is_drift else '#7ee787'}; font-weight: bold;">{obs_lang}</td>
      <td><code>{proc_name}</code></td>
      <td><code>{comm}</code></td>
      <td>{status_badge}</td>
    </tr>
    """

html_content += """
  </tbody>
</table>
</body>
</html>
"""

html_file = f"{EVIDENCE_DIR}/polyglot-endpoints-audit.html"
with open(html_file, "w") as f:
    f.write(html_content)

print("Saved polyglot-endpoints-audit.html")

# Render screenshot via Chrome headless
subprocess.run([
    "google-chrome",
    "--headless",
    "--no-sandbox",
    "--disable-gpu",
    f"--screenshot={EVIDENCE_DIR}/polyglot-endpoints-audit.png",
    "--window-size=1200,800",
    html_file
], check=True)
print("Rendered polyglot-endpoints-audit.png")
