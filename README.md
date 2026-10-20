# OAuth Setup Pilot

Built for the Composio APM Integrations take-home assignment.

**The problem:** a developer building an AI agent needs their users to connect third-party apps. Before launch, they must register an OAuth app with each provider and sometimes get it approved. Today that means piecing together documentation, unfamiliar consoles, and provider emails - with no reliable way to know if setup takes an hour or weeks.

**What this is:** a small web app that answers three questions for any OAuth provider - *where do I start, what will it cost me in time and effort, and what can block me?* It ships with a library of source-validated guides and can investigate an unfamiliar app live: submit a name or website and an agent searches official documentation, fetches it, drafts a guide, and shows its evidence and uncertainties.

## Links

- Live demo (Render free plan): https://oauth-setup-pilot.onrender.com
- Source repository: https://github.com/rohanthakur0410-art/oauth-setup-pilot

## Quickstart (local)

```bash
pip install -r requirements.txt

# Required for live guide generation (library works without it):
export GEMINI_API_KEY=...        # free-tier key from Google AI Studio
# optional:
export LLM_PROVIDER=gemini       # gemini (default) | openai | groq
export GEMINI_MODEL=gemini-3.8-flash
export GEMINI_MODEL_FALLBACKS=gemini-3.6-flash,gemini-3.5-flash   # tried in order on 429/5xx/timeout/empty
# to run on Groq's free tier instead:
# export LLM_PROVIDER=groq
# export OPENAI_API_KEY=...            # Groq API key (console.groq.com)
# export OPENAI_MODEL=openai/gpt-oss-120b
export BRAVE_API_KEY=...         # recommended: reliable search from datacenter IPs

uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

Run the checks:

```bash
python3 tests/smoke_test.py        # schema, parsers, routes - no keys needed
python3 scripts/check_sources.py   # liveness-checks every evidence URL in the library
```

## Deploy (Render, free plan)

`render.yaml` is a blueprint for a free Render web service (Python, `uvicorn app.main:app`), and the demo is deployed from it at the link above ($0/month free instance - watch the dashboard default, which preselects a paid plan). Set `GEMINI_API_KEY` (and optionally `BRAVE_API_KEY`) in the Render dashboard - never in the repo. Free-plan caveats: the service spins down after 15 idle minutes (about a minute to spin back up) and generated guides are cached on an ephemeral filesystem, so they vanish on restart. The pre-built library lives in the repo and is unaffected.

## Key decisions

1. **Evidence-first guides.** Every claim in a guide carries a source URL and a status: `docs-supported` (stated on the linked official page) or `tested` (we attempted it, e.g. an unauthenticated console reachability check). Anything not verified is listed under *Uncertainties* - never silently asserted. The library guides were hand-validated against the cited official docs on 2026-10-20, and `scripts/check_sources.py` re-verifies that every source URL is live.
2. **Anti-hallucination post-validation.** The generation agent is instructed to cite only pages it was shown, and the code enforces it: any model-cited URL that was not actually fetched is dropped from evidence and recorded as an uncertainty. Console URLs produced by the model get a live unauthenticated reachability check.
3. **Pluggable LLM and search.** `LLM_PROVIDER=gemini|openai|groq`; default model `gemini-3.8-flash` (listed as a current model on the official models page). Each request rides a fallback chain - `GEMINI_MODEL`, then `GEMINI_MODEL_FALLBACKS` - because availability flaps per model within minutes; 429 (rate limit), 5xx, timeouts and empty responses back off, retry, then move to the next model. The official Gemini API pricing page (ai.google.dev/gemini-api/docs/pricing) lists a Free Tier for the default model ("Free of charge" input), so live generation can run without billing; exact free-tier limits are shown in AI Studio. Groq is a working alternative: its docs (console.groq.com/docs/models) describe a free tier and list `openai/gpt-oss-120b` as a current production model - verified 2026-10-20 on a free-tier key, including `response_format: json_object` (its llama-3.3-70b-versatile is enterprise-gated, so the Groq default here is gpt-oss-120b).. Search uses the Brave Search API when `BRAVE_API_KEY` is set (Brave advertises a free plan of 2,000 queries/month at 1 query/second), falling back to keyless DuckDuckGo HTML plus on-site heuristics (developer/docs links discovered on the provider's own homepage). DuckDuckGo is frequently challenge-blocked from cloud IPs, which is exactly why the Brave fallback exists.
4. **Honest degradation.** Without an LLM key, live generation returns a clear 503 ("GEMINI_API_KEY is not configured") instead of faking a result. The pre-built library always works.
6. **Four independent dimensions.** Difficulty, hands-on effort, approval requirement, and external wait are separate fields, never derived from each other: `difficulty` (technical configuration complexity), `setup_time` (hands-on effort, `unknown` when undocumented - never estimated), `approval.requirement` (`required` | `conditional` | `optional` | `not-required` | `unknown`; conditional when it depends on scopes, app type, or distribution), and `approval.wait` with `approval.wait_basis` (`documented` | `estimate` | `unknown` | `none`). An app is never labeled universally approval-free when that holds only for a particular scenario.

5. **Difficulty rubric (technical complexity only).** `easy` = one short form; `medium` = several configuration surfaces but no individually hard step; `hard` = technically demanding configuration; `unknown` = the fetched documentation did not establish enough to rate it. Approval burden is rated separately (`approval.requirement`), so a provider with simple setup but a heavy review process is not "hard" to configure - it is easy/medium to configure with a conditional or required approval and its own wait. A structural guard in `app/agent.py` (`_honesty_guard`) prevents thin fetched evidence from being presented as 'no approval needed' or as a concrete setup time - unjustified categorical claims are coerced to `unknown`/`Unverified` and the guide is banner-marked as orientation, not a setup recipe. The five library apps span the space: GitHub (easy, instant), Notion (easy; optional listing review of 5-10 business days), Slack (medium; 10-workspace prerequisite + up to 10 weeks functional review), Google (medium; 3-5 business days sensitive-scope verification, weeks + annual third-party CASA assessment for restricted scopes), HubSpot (medium; 3-install prerequisite + up to 60 days total process).

## What we learned validating against real sources

- Notion's long-documented console URL `notion.so/my-integrations` now 301-redirects to `notion.so/developers/connections`; guides and tutorials citing the old path are stale (observed 2026-10-20).
- Slack's review is only triggered by Marketplace submission, but Slack states commercial distribution *requires* Marketplace submission - so "skip the review" is not a real distribution strategy.
- GitHub and Google consoles are fully login-gated (observed via unauthenticated 302 redirects); no part of registration can be inspected without an account.
- Google's brand-verification result expires after 7 days if you don't publish - a trap for teams who verify early.

## Security notes

- User-supplied URLs are reduced to their origin (scheme://host) before any use - query, fragment and path are dropped because any of them can carry secrets, and URLs with embedded credentials are rejected. The form also warns against pasting anything sensitive. This reduces but cannot eliminate the risk of a user typing a secret as free text; inputs are sent to the configured third-party LLM API (see its data-use terms).
- All outbound fetching http/https only, host must resolve to public IPs only (no private/loopback/link-local/reserved), redirects followed manually (max 3) with every hop re-checked, and downloads capped at 2 MB.
- Generation queries are restricted to a conservative character set and length.
- No secrets in the repo; API keys come from environment variables only.

## Limitations

- Live generation quality depends on the search backend and the fetched pages; sites that block bots or render docs client-side yield thin evidence, which the guide will show as uncertainties.
- Hosted LLM availability is not guaranteed at any moment. On 2026-10-20 the Gemini API intermittently returned 429, 503, empty responses, and read timeouts; full live runs succeeded earlier that day (figma.com, resend.com), while a later unfamiliar-app run (Spotify) produced an honest partial guide and its bounded retry failed on upstream errors. The deployed demo was verified the same day: health, library and guide pages all live, and a fresh unfamiliar-app run (Spotify) passed end to end - official docs discovered, guide generated, every evidence entry citing a fetched page, unknowns labelled. Later the same day the deployed instance hit upstream 429 (rate limit) and 503 (demand spike) errors across gemini-3.6-flash and gemini-3.5-flash, so the deployment now runs the fallback chain (dashboard env: GEMINI_MODEL=gemini-3.6-flash, fallbacks gemini-3.5-flash) and shows a short 'Couldn't generate the guide right now. Please try again.' with a retry link when every model is unavailable. Free-tier rate limits are per Google Cloud project and per model (RPM/TPM/RPD; RPD resets midnight Pacific - ai.google.dev/gemini-api/docs/rate-limits), so a run can still fail upstream at any moment; retry later or switch `LLM_PROVIDER`. The pre-built library is unaffected.
- The agent cannot log into provider consoles, so "attempted setup" is limited to unauthenticated reachability checks; pre-requisites inside consoles (e.g. Slack's per-app queue estimate) are out of reach without accounts.
- Generated-guide caching uses local disk, which is ephemeral on free hosting.
- No user authentication; `/api/generate` is guarded only by a per-IP allowance (5 generations / 10 minutes) and a small global concurrency cap to protect the LLM budget. Fine for a take-home demo, not for public production use.

## AI-use disclosure

This project was built with AI assistance (design, code, and research), consistent with the assignment's guidance to use agents. All library-guide claims were validated against the linked official sources during the build, and the validation tooling is included so the reviewer can re-run it.
