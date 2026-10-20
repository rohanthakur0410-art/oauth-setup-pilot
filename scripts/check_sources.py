"""Liveness-check every evidence URL in the library guides.

This is how the sample guidance is validated against real sources: every
docs-supported claim must link to an official page that actually resolves.
"""
import json
import sys
from pathlib import Path

import httpx

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
ok = True
for f in sorted(Path("guides").glob("*.json")):
    g = json.loads(f.read_text())
    for ev in g["evidence"]:
        url = ev["source"]
        try:
            r = httpx.get(url, headers=UA, timeout=25, follow_redirects=True)
            live = r.status_code < 400
        except Exception as e:
            r, live = None, False
        status = r.status_code if r else "ERR"
        print(f"{'OK ' if live else 'FAIL'} [{g['slug']}] {status} {url}")
        ok = ok and live
sys.exit(0 if ok else 1)
