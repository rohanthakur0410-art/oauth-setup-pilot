# Deploy runbook (execute only after scoped grants are in place)

## GitHub (target-bound PAT route, minimal grant)
1. Rohan creates the empty public repo `oauth-setup-pilot` at https://github.com/new (he already approved the public repo; this keeps the credential grant repo-scoped).
2. Vault-request a fine-grained PAT scoped to ONLY that repo, Contents:write. No Administration scope needed since the repo exists.
3. Push: `git remote add origin https://x-access-token:<PAT>@github.com/rohanthakur0410-art/oauth-setup-pilot.git && git push -u origin main` (token used only in the push command, never committed or logged).

## Render (agent-driven email signup, free)
1. First check for an existing account: dashboard.render.com/login with rohanthakur0410@gmail.com - do NOT silently create a duplicate.
2. If none: register with Email+Password (hCaptcha present - managed solver), generated strong password stored to vault for Rohan. Verification email read via connected Gmail.
3. New > Web Service > Public Git repository: paste the public repo URL; render.yaml blueprint sets build/start automatically (pip install -r requirements.txt; uvicorn app.main:app --host 0.0.0.0 --port $PORT).
4. Environment: LLM_PROVIDER=gemini, GEMINI_MODEL=gemini-3.8-flash, GEMINI_API_KEY from vault (dashboard env var, never in repo). Optional BRAVE_API_KEY.
5. STOP and report if any card/billing verification is demanded.
6. Verify deployed: /healthz, one library guide, one live generation; then hand the URL to Rohan for review.
