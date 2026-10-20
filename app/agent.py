"""Investigation agent: given an app name or website, produce an OAuth setup guide.

Pipeline: identify -> gather candidate official docs (search backend +
on-site heuristics) -> fetch docs -> LLM extraction -> post-validation
(evidence URLs restricted to what was actually fetched, console URL
liveness check). No fabricated citations: the model may only cite URLs it
was shown, and the post-validator enforces that.

Search backends: Brave Search API when BRAVE_API_KEY is set (reliable from
datacenter IPs), otherwise DuckDuckGo's HTML endpoint as a keyless
best-effort fallback (frequently challenge-blocked from cloud IPs).
"""
import json
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import httpx
import logging
import zlib

log = logging.getLogger("agent")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
FETCH_CAP = 9000  # chars of page text kept per fetched doc
MAX_DOCS = 4
JUNK_HOSTS = ("youtube.com", "stackoverflow", "reddit", "medium.com", "quora", "github.com")


class _DDGParser(HTMLParser):
    """Parse DuckDuckGo HTML results: <a class="result__a" href="...uddg=REAL...">"""

    def __init__(self):
        super().__init__()
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        if "result__a" not in a.get("class", ""):
            return
        href = a.get("href", "")
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            self.results.append(unquote(m.group(1)))
        elif href.startswith("http"):
            self.results.append(href)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _search_ddg(query: str, limit: int = 8) -> list[str]:
    """DuckDuckGo HTML search, no key. Returns [] when challenge-blocked."""
    try:
        r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query},
                      headers=UA, timeout=20, follow_redirects=True)
        if r.status_code != 200 or "anomaly" in r.text:
            return []
        p = _DDGParser()
        p.feed(r.text)
        seen, out = set(), []
        for u in p.results:
            if u not in seen:
                seen.add(u)
                out.append(u)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _search_brave(query: str, limit: int = 8) -> list[str]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    try:
        r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                      params={"q": query, "count": limit},
                      headers={"X-Subscription-Token": key, "Accept": "application/json"},
                      timeout=20)
        if r.status_code != 200:
            return []
        return [x["url"] for x in r.json().get("web", {}).get("results", [])][:limit]
    except Exception:
        return []


def _search(query: str, limit: int = 8) -> list[str]:
    hits = _search_brave(query, limit)
    if not hits:
        hits = _search_ddg(query, limit)
    return hits



def _sanitize_site(url: str) -> str:
    """Strip query strings, fragments and credentials from user-supplied URLs.

    A pasted URL can carry tokens or other secrets in its query (?key=...).
    Those must never reach search backends, the LLM prompt, logs, or the
    stored guide - only scheme://host[:port][/path] is kept. URLs with
    userinfo (user:pass@host) are rejected outright."""
    if "://" not in url:
        url = f"https://{url}"
    p = urlparse(url)
    if p.username or p.password:
        raise ValueError("URLs containing credentials are not accepted")
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("unsupported URL")
    # Origin only: the path is never needed (docs discovery works off the
    # homepage) and can itself carry tokens, e.g. https://app.io/<token>.
    return f"{p.scheme}://{p.netloc}"


def _registrable(host: str) -> str:
    parts = host.lower().replace("www.", "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _discover_docs(site: str) -> list[str]:
    """Keyless fallback: pull developer/docs links off the app's own homepage
    and probe common docs locations on its domain."""
    found = []
    try:
        r = _safe_get(site if site.startswith("http") else f"https://{site}", timeout=20)
        if r is None:
            raise OSError("homepage unreachable")
        p = _TextExtractor()
        p.feed(r.text[:400_000])
        base = f"{urlparse(site if site.startswith('http') else 'https://' + site).scheme}://{urlparse(site if site.startswith('http') else 'https://' + site).netloc}"
        dom = _registrable(urlparse(base).netloc)
        for href in p.links:
            u = urljoin(base, href)
            h = urlparse(u)
            if h.scheme not in ("http", "https"):
                continue
            if _registrable(h.netloc) != dom:
                continue
            if re.search(r"developer|/docs|/api|oauth", u, re.I) and u not in found:
                found.append(u)
    except Exception:
        pass
    host = urlparse(site if site.startswith("http") else f"https://{site}").netloc.replace("www.", "")
    for sub in ("developer", "developers", "docs"):
        found.append(f"https://{sub}.{host}/")
    for path in ("/developers", "/developers/docs", "/docs", "/docs/oauth", "/api/docs"):
        found.append(f"https://{host}{path}")
    return found


MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000


def _is_public_url(url: str) -> bool:
    """SSRF guard: only http(s), only hosts resolving to public IPs."""
    import ipaddress
    import socket
    h = urlparse(url)
    if h.scheme not in ("http", "https") or not h.netloc:
        return False
    host = h.hostname or ""
    infos = None
    for _attempt in range(3):  # transient DNS blips must not silently kill research
        try:
            infos = socket.getaddrinfo(host, None)
            break
        except Exception:
            time.sleep(0.4)
    if infos is None:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


class TransientFetchError(Exception):
    """All attempts failed with network-level errors (DNS/connect/timeout).
    Distinct from 'page missing/unusable' so callers can tell a temporary
    network problem from documentation that genuinely was not found."""


NETWORK_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
               httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError,
               httpx.WriteError, httpx.RemoteProtocolError,
               OSError, TimeoutError)


def _safe_get(url: str, timeout: int = 25) -> httpx.Response | None:
    """GET with manual redirect following, re-checking every hop against the
    SSRF guard, and a hard cap on downloaded bytes. Retried on transient
    network/DNS errors, which are common on shared egress. Raises
    TransientFetchError when every attempt failed at the network level;
    returns None for non-network outcomes (blocked, oversized, bad hop)."""
    net_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = _safe_get_once(url, timeout)
        except NETWORK_EXC as e:
            net_exc = e
            r = None
        if r is not None:
            return r
        time.sleep(0.5 * (attempt + 1))
    if net_exc is not None:
        raise TransientFetchError(f"network error fetching {url}: {net_exc}")
    return None


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """Tolerant body decoding. Some doc sites/CDN edges mislabel compression
    (e.g. Content-Encoding: gzip with a zlib or raw-deflate body), which makes
    httpx's automatic decoding fail. Try the advertised encoding first, then
    fall back through the common deflate variants."""
    enc = (encoding or "").lower()
    if "gzip" in enc:
        try:
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        except zlib.error:
            pass  # fall through to the deflate variants
    if "deflate" in enc or "gzip" in enc:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except zlib.error:
                continue
        raise httpx.DecodingError("body not decodable as advertised deflate/gzip")
    if "br" in enc:
        import brotli  # installed with httpx's brotli extra
        return brotli.decompress(raw)
    return raw


def _safe_get_once(url: str, timeout: int = 25) -> httpx.Response | None:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _is_public_url(current):
            log.warning("fetch blocked by SSRF guard: %s", current)
            return None
        try:
            with httpx.stream("GET", current, headers=UA, timeout=timeout,
                              follow_redirects=False) as r:
                if r.is_redirect:
                    loc = r.headers.get("location")
                    if not loc:
                        log.warning("redirect without Location: %s", current)
                        return None
                    current = urljoin(current, loc)
                    continue
                chunks, total = [], 0
                for chunk in r.iter_raw(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        log.warning("response too large, aborted: %s", current)
                        return None
                    chunks.append(chunk)
                raw = b"".join(chunks)
                body = _decode_body(raw, r.headers.get("content-encoding", ""))
                if len(body) > MAX_BYTES:
                    log.warning("decoded body too large, aborted: %s", current)
                    return None
                headers = dict(r.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(r.status_code, headers=headers, content=body)
        except NETWORK_EXC:
            raise
        except Exception as e:
            log.warning("fetch failed %s: %r", current, e)
            return None
    log.warning("too many redirects from %s", url)
    return None


def _fetch_text(url: str) -> str | None:
    r = _safe_get(url, timeout=25)
    if r is None or r.status_code >= 400:
        return None
    if "text/html" not in r.headers.get("content-type", ""):
        return None
    p = _TextExtractor()
    p.feed(r.text[:400_000])
    return p.text()[:FETCH_CAP]


def _head_check(url: str) -> dict:
    """Record what an unauthenticated reachability attempt saw."""
    if not _is_public_url(url):
        return {"url": url, "status": None, "error": "blocked by SSRF guard"}
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=False)
        return {"url": url, "status": r.status_code,
                "redirect": r.headers.get("location")}
    except Exception as e:
        return {"url": url, "status": None, "error": str(e)[:200]}


def _llm(prompt: str) -> str:
    """Call the configured LLM. Raises RuntimeError with a clear message if unconfigured."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured; live guide generation is unavailable.")
        model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}},
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if provider in ("openai", "groq", "openai-compatible"):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured; live guide generation is unavailable.")
        base = os.environ.get("OPENAI_BASE_URL",
                              "https://api.groq.com/openai" if provider == "groq" else "https://api.openai.com")
        model = os.environ.get("OPENAI_MODEL", "llama-3.3-70b-versatile" if provider == "groq" else "gpt-4o-mini")
        r = httpx.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}, "temperature": 0.2},
            timeout=90,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}'. Use gemini, openai, or groq.")


PROMPT = """You are investigating how a developer creates and gets approval for an OAuth app with the provider below, so their AI-agent product's users can connect that app.

APP INPUT: {query}
RESOLVED SITE: {site}

Below is text actually fetched from official documentation pages, each labeled with its URL. Base every factual claim ONLY on this fetched text. If something important is not covered by the fetched text, put it in "uncertainties" instead of guessing.

=== FETCHED DOCUMENTATION ===
{docs}
=== END ===

Produce a JSON object with EXACTLY these keys:
{{
  "app_name": string,
  "website": string (canonical marketing site),
  "developer_console": string (URL where the OAuth app is registered; from the docs if present),
  "difficulty": "easy" | "medium" | "hard" | "unknown",
  "difficulty_reason": string (one sentence; easy=no approval wait, medium=optional/short review, hard=mandatory review or verification with real wait, unknown=the fetched docs do not establish the approval/review facts needed to rate it),
  "setup_time": string (hands-on time to register the app, e.g. "~15 minutes"; use "unknown" if the fetched docs do not state it - never estimate),
  "approval": {{"required": true | false | null, "kind": string (e.g. "none", "marketplace review", "scope verification + security assessment", "unknown"), "wait": string (e.g. "none", "3-5 business days", "up to 10 weeks", "unknown"), "notes": string}},
  "summary": string (2-3 sentences a developer can act on),
  "steps": [{{"title": string, "detail": string}}],
  "requirements": [string],
  "nuances": [string] (things that bite people: scope tiers, redirect URL rules, review prerequisites, video requirements),
  "common_blockers": [string],
  "agent_can_do": [string] (what an agent/assistant can do for the developer),
  "human_must_do": [string] (what only the developer can do: accounts, payments, legal, submitting),
  "evidence": [{{"claim": string, "source": string (MUST be one of the fetched URLs above), "status": "docs-supported"}}],
  "uncertainties": [string]
}}

Rules: every evidence.source must be copied verbatim from the fetched URLs. Difficulty must follow the rubric. ABSENCE OF INFORMATION IS NOT EVIDENCE OF ABSENCE: if the fetched text does not explicitly state whether a review/approval/verification is required, set approval.required to null, approval.kind to "unknown", approval.wait to "unknown" and difficulty to "unknown" - never conclude "no approval needed" from missing documentation. If essential setup specifics (redirect URI rules, scopes, credential handling) are not in the fetched text, keep steps to what is documented and name the missing specifics in uncertainties. Return ONLY the JSON object."""


def _honesty_guard(guide: dict, fetched: dict) -> None:
    """Structural honesty check (not model-dependent): a thin corpus cannot
    establish 'no approval needed' or a concrete setup time. Coerces
    unjustified categorical claims to unknown and records why."""
    corpus = "\n".join(fetched.values()).lower()
    approval_terms = len(re.findall(r"review|approval|verification|listing|marketplace", corpus))
    ap = guide.get("approval") or {}
    if ap.get("required") is False and approval_terms < 3:
        ap["required"] = None
        ap["kind"] = "unknown"
        ap["wait"] = "unknown"
        guide.setdefault("uncertainties", []).append(
            "The fetched documentation did not establish whether an approval or review is required; "
            "absence of a mention in limited documentation is not evidence that none is required.")
    if not isinstance(ap.get("required"), bool):
        ap.setdefault("kind", "unknown")
        if ap.get("wait") in (None, "", "none"):
            ap["wait"] = "unknown"
    guide["approval"] = ap
    if re.search(r"\d", str(guide.get("setup_time") or "")) and not re.search(
            r"\b\d+\s*(minute|hour|day|week)s?\b|business days", corpus):
        guide["setup_time"] = "unknown"
        guide.setdefault("uncertainties", []).append(
            "No hands-on setup time is stated in the fetched documentation, so no estimate is given.")
    if guide.get("difficulty") == "easy" and not isinstance(ap.get("required"), bool):
        guide["difficulty"] = "unknown"
        guide["difficulty_reason"] = ("The fetched documentation does not establish whether an approval "
                                      "or review is required, so difficulty cannot be rated responsibly.")


def investigate(query: str) -> dict:
    """Run the full pipeline. Returns a guide dict with provenance metadata."""
    started = time.time()
    query = query.strip()
    if not query:
        raise ValueError("empty query")
    if re.match(r"^https?://", query) or re.match(r"^[\w-]+(\.[\w-]+)+$", query):
        site = _sanitize_site(query)
        name = urlparse(site).netloc.replace("www.", "")
    else:
        name = query
        hits = _search(f"{query} official website")
        site = _sanitize_site(hits[0]) if hits else ""

    candidates: list[str] = []
    for q in (f"{name} OAuth app registration developer documentation",
              f"{name} OAuth app review approval verification process developers",
              f"{name} create OAuth app redirect URI developer console"):
        candidates.extend(_search(q, limit=6))
    if site:
        candidates.extend(_discover_docs(site))

    # Prefer official-domain URLs, then the rest; dedupe, skip junk hosts.
    dom = _registrable(urlparse(site).netloc) if site else ""
    def rank(u):
        h = urlparse(u).netloc
        return 0 if dom and _registrable(h) == dom else 1
    seen, ordered = set(), []
    for u in sorted(candidates, key=rank):
        if u in seen or any(b in urlparse(u).netloc for b in JUNK_HOSTS):
            continue
        seen.add(u)
        ordered.append(u)

    fetched: dict[str, str] = {}
    net_fail = False
    for u in ordered:
        try:
            txt = _fetch_text(u)
        except TransientFetchError:
            net_fail = True
            continue
        if txt and len(txt) > 400:
            fetched[u] = txt
        if len(fetched) >= MAX_DOCS:
            break

    # One-level deepen: when the corpus barely mentions OAuth, follow
    # OAuth-looking links from the fetched pages themselves (same domain only).
    def _oauth_mentions(t: str) -> int:
        return len(re.findall(r"oauth|authorization", t, re.I))
    if sum(1 for t in fetched.values() if _oauth_mentions(t) >= 3) < 2 and site:
        extra: list[str] = []
        for u in list(fetched):
            try:
                r = _safe_get(u, timeout=20)
            except TransientFetchError:
                continue
            if r is None or "text/html" not in r.headers.get("content-type", ""):
                continue
            p = _TextExtractor()
            p.feed(r.text[:400_000])
            for href in p.links:
                v = urljoin(u, href)
                h = urlparse(v)
                if (h.scheme in ("http", "https") and _registrable(h.netloc) == dom
                        and re.search(r"oauth|authorization|authentication", v, re.I)
                        and v not in fetched and v not in extra):
                    extra.append(v)
            if len(extra) >= 6:
                break
        for v in extra[:3]:
            try:
                t = _fetch_text(v)
            except TransientFetchError:
                continue
            if t and len(t) > 400:
                fetched[v] = t

    if not fetched:
        if net_fail:
            raise RuntimeError(
                f"Temporary network problem: could not reach any documentation for '{query}'. "
                "Requests to candidate documentation pages failed at the network level "
                "(DNS/connect/timeout). This is usually transient - wait a moment and retry.")
        raise RuntimeError(
            f"Could not fetch any usable documentation for '{query}'. "
            "The app's docs may be unreachable from this server, behind JavaScript rendering, "
            "or the app name may not have resolved to an official site. Check the name/URL and retry.")

    docs_block = "\n\n".join(f"--- URL: {u} ---\n{t}" for u, t in fetched.items())
    raw = _llm(PROMPT.format(query=query, site=site, docs=docs_block))
    guide = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())

    # Post-validation: evidence may only cite URLs we actually fetched.
    valid_evidence, dropped = [], 0
    for ev in guide.get("evidence", []):
        if ev.get("source") in fetched:
            valid_evidence.append(ev)
        else:
            dropped += 1
    guide["evidence"] = valid_evidence
    if dropped:
        guide.setdefault("uncertainties", []).append(
            f"{dropped} model-cited source(s) were dropped because they were not among the pages actually fetched.")

    _honesty_guard(guide, fetched)

    # Console liveness check (unauthenticated attempt).
    console = guide.get("developer_console") or ""
    if console.startswith("http"):
        check = _head_check(console)
        guide["console_check"] = check
        if check.get("status") is None or (check.get("status") or 500) >= 400:
            guide.setdefault("uncertainties", []).append(
                f"Console URL {console} did not resolve cleanly in an unauthenticated check.")

    guide.update({
        "slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        "generated": True,
        "query": query,
        "sources_fetched": list(fetched.keys()),
        "generation_seconds": round(time.time() - started, 1),
        "llm_provider": os.environ.get("LLM_PROVIDER", "gemini"),
    })
    return guide
