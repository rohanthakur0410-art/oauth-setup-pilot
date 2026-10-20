# Deploy runbook (execute only after scoped grants are in place)

## GitHub - preferred: agent-driven sign-in (repo/push are our execution work under the existing grant)
1. Vault-request a GitHub login (username rohanthakur0410-art, password + 2FA method), scoped to github.com.
2. Cloud browser: sign in to github.com. If 2FA challenges, read the code from the connected authenticator/email source; only ask Rohan if no source can supply it.
3. Create the approved public repo `oauth-setup-pilot` via the UI or `gh`/API with the session; push: `git remote add origin https://github.com/rohanthakur0410-art/oauth-setup-pilot.git && git push -u origin main` (browser git credential helper or one-off header; token/password never committed or logged).
4. Fallback ONLY if sign-in is genuinely unavailable (e.g. passkey-only account, CAPTCHA loop): ask Rohan to create the empty repo at https://github.com/new, then vault-request a fine-grained PAT scoped to ONLY that repo, Contents:write, and push. Explain the actual blocker, not speculation.

## Render (agent-driven email signup, free)
1. First check for an existing account: dashboard.render.com/login with rohanthakur0410@gmail.com - do NOT silently create a duplicate.
2. If none: register with Email+Password (hCaptcha present - managed solver), generated strong password stored to vault for Rohan. Verification email read via connected Gmail.
3. New > Web Service > Public Git repository: paste the public repo URL; render.yaml blueprint sets build/start automatically (pip install -r requirements.txt; uvicorn app.main:app --host 0.0.0.0 --port $PORT).
4. Environment: LLM_PROVIDER=gemini, GEMINI_MODEL=gemini-3.8-flash, GEMINI_API_KEY from vault (dashboard env var, never in repo). Optional BRAVE_API_KEY.
5. STOP and report if any card/billing verification is demanded.
6. Verify deployed: /healthz, one library guide, one live generation; then hand the URL to Rohan for review.

Readiness honesty: unknowns remain (GitHub 2FA path, Render signup challenges, first real-LLM latency/quality on Render free tier). Sequence them one at a time and report actual blockers.
