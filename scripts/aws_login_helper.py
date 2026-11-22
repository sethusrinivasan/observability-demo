#!/usr/bin/env python3
import subprocess
import sys
import os
import time
import re

AUTH_CODE_FILE = "/tmp/aws_auth_code.txt"
LOGIN_URL_FILE = "/tmp/aws_login_url.txt"
STATUS_FILE = "/tmp/aws_login_status.txt"
LOG_FILE = "/tmp/aws_login.log"

# Clean up previous state
for f in [AUTH_CODE_FILE, LOGIN_URL_FILE, STATUS_FILE, LOG_FILE]:
    if os.path.exists(f):
        try:
            os.remove(f)
        except OSError:
            pass

with open(LOG_FILE, "w") as log:
    log.write(f"[{time.ctime()}] Starting aws login --remote...\n")
    log.flush()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        ["/home/home/.local/bin/aws", "login", "--remote"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
        env=env
    )

    os.set_blocking(proc.stdout.fileno(), False)

    url = None
    accumulated = ""

    # Read until we see the prompt or URL
    start = time.time()
    while time.time() - start < 30:
        try:
            chunk = proc.stdout.read()
        except BlockingIOError:
            chunk = None
        if chunk:
            accumulated += chunk
            log.write(chunk)
            log.flush()
            
            match = re.search(r'(https://[^\s]+)', accumulated)
            if match and not url:
                url = match.group(1)
                with open(LOGIN_URL_FILE, "w") as uf:
                    uf.write(url + "\n")
                log.write(f"\n[HELPER] Detected URL: {url}\n")
                log.flush()
                
            if "Enter the authorization code" in accumulated:
                log.write("\n[HELPER] Detected prompt for authorization code.\n")
                log.flush()
                break
        time.sleep(0.1)

    if not url:
        with open(STATUS_FILE, "w") as sf:
            sf.write("FAILED: No URL received from aws login --remote\n")
        log.write("FAILED: No URL received\n")
        log.flush()
        sys.exit(1)

    print(f"URL_READY: {url}", flush=True)

    # Now wait for AUTH_CODE_FILE
    log.write(f"[HELPER] Waiting for {AUTH_CODE_FILE} (timeout 900s)...\n")
    log.flush()

    timeout = 900
    start = time.time()
    code = None
    while time.time() - start < timeout:
        if os.path.exists(AUTH_CODE_FILE):
            time.sleep(0.5)
            with open(AUTH_CODE_FILE, "r") as cf:
                code = cf.read().strip()
            if code:
                log.write(f"[HELPER] Read code of length {len(code)}\n")
                log.flush()
                break
        time.sleep(1)

    if not code:
        log.write("[HELPER] Timed out waiting for auth code file.\n")
        proc.kill()
        with open(STATUS_FILE, "w") as sf:
            sf.write("FAILED: Timed out waiting for auth code\n")
        sys.exit(1)

    # Send code to proc.stdin
    log.write("[HELPER] Sending code to stdin...\n")
    log.flush()
    proc.stdin.write(code + "\n")
    proc.stdin.flush()

    # Reset blocking to read remaining output
    os.set_blocking(proc.stdout.fileno(), True)
    rest_out = proc.stdout.read()
    proc.wait(timeout=30)
    log.write(rest_out or "")
    log.flush()

    ret = proc.returncode
    log.write(f"\n[HELPER] Process exited with returncode {ret}\n")
    log.flush()

    if ret == 0:
        with open(STATUS_FILE, "w") as sf:
            sf.write("SUCCESS\n")
        print("LOGIN_SUCCESS", flush=True)
    else:
        with open(STATUS_FILE, "w") as sf:
            sf.write(f"FAILED: Exit code {ret}\n{rest_out}\n")
        print(f"LOGIN_FAILED: {ret}", flush=True)
        sys.exit(ret)
