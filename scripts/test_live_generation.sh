#!/usr/bin/env bash
# Real-LLM end-to-end test. Requires GEMINI_API_KEY in env. No secrets are
# printed or persisted by this script.
set -euo pipefail
cd "$(dirname "$0")/.."
export GENERATED_DIR=/tmp/gen_e2e
uvicorn app.main:app --port 8770 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 3
echo '--- health ---'
curl -s http://localhost:8770/healthz
echo; echo '--- generate figma.com ---'
curl -s -m 180 -X POST http://localhost:8770/api/generate \
  -H 'Content-Type: application/json' -d '{"query":"figma.com"}' \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "error" in d: print("ERROR:", d["error"]); sys.exit(1)
g=d["guide"]
print("slug:", g["slug"], "| difficulty:", g["difficulty"], "| seconds:", g["generation_seconds"])
print("console:", g["developer_console"], "| check:", g.get("console_check",{}).get("status"))
print("fetched:", len(g["sources_fetched"]), "evidence:", len(g["evidence"]), "uncertainties:", len(g["uncertainties"]))
bad=[e for e in g["evidence"] if e["source"] not in g["sources_fetched"]]
print("citations-not-fetched:", len(bad))
assert not bad, bad
print("E2E PASS")
'
