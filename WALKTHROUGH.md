# Walkthrough: how to explain this project

Read this before submitting. It is the story of the project in plain words - what was built, why, and what you should be able to answer if Composio asks about it.

## The one-paragraph version

OAuth Setup Pilot answers a question every agent builder hits before launch: "what will it actually take to register and approve an OAuth app with this provider?" It has two parts. First, a library of ready guides for well-known apps, where every claim links to the official doc it came from and is labeled docs-supported or tested. Second, a live flow: you type an app name or website, an agent finds the provider's official documentation, reads it, and drafts the same kind of guide in about a minute - with the sources it used and the things it could not verify shown explicitly.

## Decisions you should be able to defend

1. **Why evidence-first guides?** Because the failure mode of LLM-generated guidance is confident, wrong claims. So every claim in a guide carries its source URL and a status. Anything the evidence does not support goes in an "Uncertainties" section instead of being smoothed over. That mirrors the assignment: "Be clear about what you tested, what sources support, and what remains uncertain."

2. **Why does the agent drop citations?** The model is told to cite only pages it was actually shown - and the code enforces it. After generation, any evidence entry whose URL was not in the fetched set is removed and recorded as an uncertainty. There is also a live reachability check on the console URL the model names. This is the piece that makes the live demo trustworthy.

3. **Why these five library apps?** They span the real difficulty range: GitHub (no approval at all), Notion (OAuth works immediately, listing review optional), Slack (review required for commercial distribution, 10-workspace prerequisite, up to 10 weeks), Google (scope-tiered verification: minutes for branding, 3-5 business days for sensitive scopes, weeks plus an annual third-party security assessment for restricted scopes), HubSpot (needs 3 real installs before you can even submit, up to 60 days total). The point is that "OAuth setup" is not one problem - it is a spectrum from a 10-minute form to a months-long compliance process.

4. **Why pluggable LLM and search?** The app should not die because one vendor changes something. The LLM provider is an env var (Gemini default, OpenAI/Groq compatible), and the search layer falls back from a keyed API (Brave) to keyless DuckDuckGo plus heuristics that find developer/docs links on the provider's own homepage.

5. **Why the security notes matter.** The generate endpoint fetches URLs discovered on the open web - a classic SSRF surface. All fetching goes through a guard: public IPs only, redirects followed manually with each hop re-checked, downloads capped. The endpoint is rate-limited per IP to protect the LLM budget.

## What was learned by validating against real sources

- Notion's console URL moved: notion.so/my-integrations now redirects to notion.so/developers/connections. Tutorials citing the old URL are stale.
- Slack: you only hit the review if you distribute commercially, but Slack says commercial distribution requires the Marketplace - there is no "skip review" path to real distribution.
- Google: brand verification results expire in 7 days if you don't publish - verifying too early is a trap. Restricted scopes mean an annual third-party CASA assessment, every year, not once.
- HubSpot: you need 3 active, unaffiliated installs *before* you can submit - the chicken-and-egg is the real blocker.

## Limits you should state honestly if asked

- Live generation quality depends on what the agent can fetch; bot-blocked or JS-only docs thin out the evidence, and the guide says so.
- The agent cannot log into provider consoles, so it cannot attempt the actual registration forms.
- Generated guides are cached on ephemeral disk on free hosting; the library is permanent because it lives in the repo.
- The hosted LLM API can be intermittently unavailable: on 2026-10-20 it returned 429/503/empty responses and timeouts at times. Two full live runs succeeded earlier that day; a Spotify run produced an honest partial and its bounded retry failed upstream. If a live demo run fails on camera, say exactly that - the evidence pipeline is the substance, and the library always works.

## What is verified vs what is pending (say this exactly)

Verified by automated checks in the repo (anyone can re-run them):
- `python3 tests/smoke_test.py` - guide schema, HTML parsers, routes, SSRF guard, URL sanitizer, export.
- `python3 scripts/check_sources.py` - every evidence URL in the five library guides resolves live today.

Verified during the build (2026-10-20): all library claims checked against the linked official docs; full live end-to-end generations ran on the real key for two unfamiliar apps (figma.com and resend.com) - official docs discovered and fetched, guide generated, evidence entries semantically verified against the fetched source text; a third unfamiliar app (Spotify) produced an honest partial guide that labelled its missing evidence as uncertainties; the anti-hallucination filter was exercised with a mock model response and dropped citations to pages that were never fetched; console URLs were reachability-tested unauthenticated.

Pending at any given moment: a fresh live run succeeding right now (the hosted Gemini API was returning 429/503/empty responses and timeouts at times on 2026-10-20, so a given attempt may fail upstream - if asked, say two full live runs succeeded earlier that day and availability is intermittent), and the public deployed URL. Never present a guide as freshly generated if it was generated earlier; never promise a review timeline a provider has not documented.

## If they ask "how much of this did AI write?"

Answer honestly: the project was built with agentic coding, which the assignment explicitly invites ("Use agents to investigate...", and the email's "comfort with agentic coding"). The design decisions, the evidence discipline, and the validation against real sources are the substance - and every library claim can be re-checked with `python3 scripts/check_sources.py`.
