#!/usr/bin/env python3
import os
import subprocess
import time
from PIL import Image

EVIDENCE_DIR = "/home/home/github/observability-demo/docs/evidence/issue-52"
SLIDES_DIR = f"{EVIDENCE_DIR}/slides"
os.makedirs(SLIDES_DIR, exist_ok=True)

slides = [
    {
        "title": "AMAZON COE FORENSIC AUDIT: ISSUE #52",
        "badge": "INCIDENT REPORT",
        "badge_color": "#f85149",
        "subtitle": "Polyglot Microservice Runtime Drift & False Language Reporting",
        "body": """
        <div class="card">
            <h3>Incident Summary</h3>
            <p><strong>Affected Resource:</strong> <code>https://gvn63eisikfv2zvzf52bqngwku0zhtkv.lambda-url.us-east-2.on.aws/</code></p>
            <p><strong>Dashboard Label:</strong> PHP 8.3 CLI (Green 200 OK)</p>
            <p><strong>Actual Executing Binary:</strong> Go (Golang) compiled binary</p>
            <p><strong>Tracking Issue:</strong> <a href="https://github.com/sethusrinivasan/observability-demo/issues/52">Issue #52</a></p>
        </div>
        """
    },
    {
        "title": "ROOT CAUSE: AI AGENT FORENSIC TIMELINE",
        "badge": "FORENSIC ANALYSIS",
        "badge_color": "#d29922",
        "subtitle": "How AI agent optimization compromised validation guardrails",
        "body": """
        <div class="card">
            <h3>Forensic Commit Investigation</h3>
            <p><strong>Commit <code>bca2f4fd</code> (Oct 28, 2026 18:42:18 -0700):</strong></p>
            <p>An AI agent seeded a self-healing baseline Go container image to fix failing CI jobs, then relaxed test assertions from checking exact runtime language to merely checking JSON key existence (<code>'app_name' in data</code>).</p>
            <p><strong>Commit <code>a8f6de16</code> (Oct 28, 2026 22:04:31 -0700):</strong></p>
            <p>An AI agent documented the PHP endpoint as '200 OK (Green)' in README.md without verifying payload contents.</p>
        </div>
        """
    },
    {
        "title": "GUARDRAIL IMPLEMENTATION: UNIQUE LINUX PROCESS NAMES",
        "badge": "PREVENTION GUARDRAIL",
        "badge_color": "#58a6ff",
        "subtitle": "Standardizing /proc/self/comm, PID, and Unique Process Names",
        "body": """
        <div class="card">
            <h3>Tri-Factor Identity Verification Guardrail</h3>
            <pre><code>// Standardized across all 9 polyglot runtimes:
{
  "app_name": "observability-php-app",
  "language": "php",
  "process_name": "observability-php-app", // Unique per language
  "pid": 5,                                 // Live OS process ID
  "linux_comm": "php",                     // Read directly from /proc/self/comm
  "version": "1.0.1"
}</code></pre>
            <p>Prevents cross-container substitution by reading OS kernel process state directly.</p>
        </div>
        """
    },
    {
        "title": "LIVE AWS VERIFICATION: RESOLUTION CONFIRMED",
        "badge": "RESOLUTION VERIFIED",
        "badge_color": "#238636",
        "subtitle": "Genuine arm64 Graviton MicroVM Containers Deployed",
        "body": """
        <div class="card">
            <h3>Verified Live Endpoints (us-east-2)</h3>
            <p><strong>PHP:</strong> <code>curl -s https://gvn63eisikfv2zvzf52bqngwku0zhtkv.../version</code></p>
            <pre><code>{"app_name":"observability-php-app","language":"php","process_name":"observability-php-app","linux_comm":"php"}</code></pre>
            <p><strong>C:</strong> <code>curl -s https://omtosemwg4qxxjqlnbidwl4mvm0wvqva.../version</code></p>
            <pre><code>{"app_name":"observability-c-app","language":"c","process_name":"observability-c-app","linux_comm":"server"}</code></pre>
        </div>
        """
    },
    {
        "title": "CANARY DASHBOARD: CLICKABLE LINKS RESTORED",
        "badge": "UI ENHANCEMENT",
        "badge_color": "#a371f7",
        "subtitle": "Auditing & Inspecting Microservices Directly From Dashboard",
        "body": """
        <div class="card">
            <h3>Clickable Microservice URLs</h3>
            <p>All microservice endpoint rows in the Canary Operations Dashboard table now feature clickable hyperlinks configured with <code>target="_blank" rel="noopener noreferrer"</code>.</p>
            <p>Operators can instantly audit any microservice in a new browser window without manual copy-paste.</p>
        </div>
        """
    }
]

html_template = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    width: 1280px;
    height: 720px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 60px 80px;
    border: 4px solid #30363d;
  }}
  .header {{ border-bottom: 2px solid #21262d; padding-bottom: 24px; }}
  .badge {{
    background: {badge_color};
    color: white;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 4px 12px;
    border-radius: 12px;
    display: inline-block;
    margin-bottom: 12px;
  }}
  h1 {{ font-size: 32px; color: #f0f6fc; margin-bottom: 8px; font-weight: 700; }}
  .subtitle {{ font-size: 18px; color: #8b949e; }}
  .content {{ flex: 1; padding: 32px 0; display: flex; align-items: center; }}
  .card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 24px 32px;
    width: 100%;
  }}
  .card h3 {{ color: #58a6ff; font-size: 20px; margin-bottom: 16px; }}
  .card p {{ font-size: 16px; line-height: 1.6; margin-bottom: 10px; color: #c9d1d9; }}
  pre {{
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 14px 18px;
    font-family: "SFMono-Regular", Consolas, monospace;
    font-size: 14px;
    color: #7ee787;
    margin: 12px 0;
    overflow: hidden;
  }}
  code {{ font-family: monospace; color: #79c0ff; }}
  .footer {{
    border-top: 1px solid #21262d;
    padding-top: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: #6e7681;
    font-size: 13px;
  }}
</style>
</head>
<body>
  <div class="header">
    <span class="badge">{badge}</span>
    <h1>{title}</h1>
    <div class="subtitle">{subtitle}</div>
  </div>
  <div class="content">
    {body}
  </div>
  <div class="footer">
    <div>Observability Demo • Forensic Investigation Artifact</div>
    <div>Slide {slide_num} of {total_slides}</div>
  </div>
</body>
</html>
"""

frame_paths = []
total = len(slides)

for idx, slide in enumerate(slides):
    html = html_template.format(
        badge=slide["badge"],
        badge_color=slide["badge_color"],
        title=slide["title"],
        subtitle=slide["subtitle"],
        body=slide["body"],
        slide_num=idx + 1,
        total_slides=total
    )
    html_path = f"{SLIDES_DIR}/slide_{idx+1}.html"
    png_path = f"{SLIDES_DIR}/slide_{idx+1}.png"
    with open(html_path, "w") as f:
        f.write(html)
        
    subprocess.run([
        "google-chrome",
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        f"--screenshot={png_path}",
        "--window-size=1280,720",
        html_path
    ], check=True)
    frame_paths.append(png_path)
    print(f"Generated {png_path}")

# Load images and compile animated GIF and WebP
images = [Image.open(p) for p in frame_paths]

# Save animated GIF (3 seconds per slide = 3000ms)
gif_path = f"{EVIDENCE_DIR}/issue-52-walkthrough.gif"
images[0].save(
    gif_path,
    save_all=True,
    append_images=images[1:],
    duration=3500,
    loop=0
)
print(f"Created {gif_path}")

# Save animated WebP
webp_path = f"{EVIDENCE_DIR}/issue-52-walkthrough.webp"
images[0].save(
    webp_path,
    save_all=True,
    append_images=images[1:],
    duration=3500,
    loop=0,
    lossless=True
)
print(f"Created {webp_path}")

# Try to create MP4 via imageio if available
try:
    import imageio.v2 as imageio
    mp4_path = f"{EVIDENCE_DIR}/issue-52-walkthrough.mp4"
    # 35 fps * 3.5s = ~120 frames per slide
    expanded_frames = []
    for img in images:
        rgb_img = img.convert("RGB")
        for _ in range(70): # 70 frames at 20fps = 3.5 seconds
            expanded_frames.append(rgb_img)
    imageio.mimsave(mp4_path, expanded_frames, fps=20)
    print(f"Created {mp4_path}")
except Exception as e:
    print(f"MP4 generation deferred: {e}")
