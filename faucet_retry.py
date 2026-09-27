#!/usr/bin/env python3
"""Retry the Kryvora faucet every N seconds until one claim is accepted.

The faucet enforces a per-IP window, so this simply keeps trying (each accepted
attempt costs one 2captcha solve).

    python3 faucet_retry.py 0xADDRESS [interval_seconds] [max_attempts]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kryvora_bot as kb  # noqa: E402

addr = sys.argv[1]
interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
max_attempts = int(sys.argv[3]) if len(sys.argv) > 3 else 24

key = kb.os.getenv("TWOCAPTCHA_KEY", "")
solver = kb.CaptchaSolver(key)

for i in range(1, max_attempts + 1):
    kb.log(f"attempt {i}/{max_attempts} for {addr}", "STEP")
    token = solver.solve_turnstile(kb.FAUCET + "/", kb.TURNSTILE_SITEKEY)
    if token:
        import requests

        s = requests.Session()
        s.headers.update({
            "Content-Type": "application/json",
            "Origin": kb.FAUCET,
            "Referer": kb.FAUCET + "/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        })
        r = s.post(kb.FAUCET + "/api/faucet", json={"address": addr, "turnstileToken": token}, timeout=45)
        d = r.json()
        kb.log(f"response {r.status_code}: {str(d)[:200]}", "OK" if d.get("ok") else "WARN")
        if d.get("ok"):
            print("SUCCESS", d.get("tx"))
            sys.exit(0)
        if "rate limit" not in str(d).lower():
            kb.log("non-rate-limit error — stopping", "ERR")
            sys.exit(2)
    if i < max_attempts:
        time.sleep(interval)
print("GAVE_UP")
sys.exit(1)
