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
import base64

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


class _BingParser(HTMLParser):
    """Parse Bing HTML results: <h2><a href="REAL"> inside <li class="b_algo">.
    Bing wraps some links in redirect/tracking URLs; keep only direct http(s)."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._in_algo = 0
        self._in_h2 = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li" and "b_algo" in a.get("class", ""):
            self._in_algo += 1
        elif tag == "h2" and self._in_algo:
            self._in_h2 += 1
        elif tag == "a" and self._in_algo and self._in_h2:
            href = a.get("href", "")
            # Bing wraps results in /ck/a redirect URLs; the real destination
            # is base64url-encoded in the u param after an "a1" prefix.
            m = re.search(r"[?&]u=a1([^&]+)", href)
            if m:
                s = m.group(1) + "=" * (-len(m.group(1)) % 4)
                try:
                    href = base64.urlsafe_b64decode(s).decode("utf-8", "replace")
                except Exception:
                    return
            if href.startswith("http") and "bing.com" not in urlparse(href).netloc:
                self.results.append(href)

    def handle_endtag(self, tag):
        if tag == "li" and self._in_algo:
            self._in_algo -= 1
        elif tag == "h2" and self._in_h2:
            self._in_h2 -= 1


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


def _search_bing(query: str, limit: int = 8) -> list[str]:
    """Bing HTML search, no key. Fallback when DDG challenge-blocks this egress IP."""
    try:
        r = httpx.get("https://www.bing.com/search", params={"q": query},
                      headers=UA, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return []
        p = _BingParser()
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
    if not hits:
        hits = _search_bing(query, limit)
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
    # Probe common doc locations on each likely developer host. Search engines
    # often rank marketing pages above docs, so direct probing is the reliable
    # keyless discovery path.
    doc_paths = ("/documentation", "/docs", "/docs/oauth", "/docs/authentication",
                 "/docs/authorization", "/documentation/oauth", "/oauth", "/api/docs")
    for sub in ("developer", "developers", "docs"):
        found.append(f"https://{sub}.{host}/")
        for path in doc_paths:
            found.append(f"https://{sub}.{host}{path}")
    for path in ("/developers", "/developers/docs", "/docs", "/documentation",
                 "/docs/oauth", "/docs/authentication", "/api/docs"):
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


def _model_chain() -> list[str]:
    """Primary model plus fallbacks, tried in order when a model is unavailable.

    Provider 503s are explicitly demand spikes ("high demand ... temporary"),
    and availability flaps per model within minutes, so a single user request
    should ride across models instead of dying on the first spike.
    """
    primary = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    fallbacks = [m.strip() for m in os.environ.get(
        "GEMINI_MODEL_FALLBACKS", "gemini-3.6-flash,gemini-3.5-flash").split(",") if m.strip()]
    chain = [primary] + [m for m in fallbacks if m != primary]
    return chain


def _llm(prompt: str) -> str:
    """Call the configured LLM, riding across the model chain on transient
    failures (network errors, 5xx, empty responses). Raises a sanitized
    RuntimeError - never leaks endpoint URLs or exception internals."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    models = _model_chain() if provider == "gemini" else [None]
    for model in models:
        for attempt in range(2):
            try:
                return _llm_once(prompt, provider, model=model)
            except NETWORK_EXC as e:
                log.warning("LLM network error (model=%s attempt %d): %r", model, attempt + 1, e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise
                log.warning("LLM server error %d (model=%s attempt %d)",
                            e.response.status_code, model, attempt + 1)
            except (KeyError, IndexError, ValueError) as e:
                log.warning("LLM empty/unparseable response (model=%s attempt %d): %r", model, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM_UNAVAILABLE")


def _llm_once(prompt: str, provider: str, model: str | None = None) -> str:
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured; live guide generation is unavailable.")
        model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
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
  "difficulty_reason": string (one sentence on TECHNICAL COMPLEXITY ONLY: easy=one short form, medium=several configuration surfaces but no hard step, hard=technically demanding configuration; approval burden is rated separately in approval.requirement and must NOT drive difficulty),
  "setup_time": string (hands-on time to register the app, e.g. "~15 minutes"; use "unknown" if the fetched docs do not state it - never estimate),
  "approval": {{"required": true | false | null, "requirement": "required" | "conditional" | "optional" | "not-required" | "unknown", "kind": string (e.g. "none", "marketplace review", "scope verification + security assessment", "unknown"), "wait": string (e.g. "none", "3-5 business days", "up to 10 weeks", "unknown"), "wait_basis": "documented" | "estimate" | "unknown" | "none", "notes": string}},
  requirement meanings: "required" = a review/verification is mandatory before the OAuth app works for its intended use; "conditional" = it depends on requested scopes, app type, or distribution model (explain the dependency in notes); "optional" = a review exists but the OAuth app works without it; "not-required" = ONLY when the fetched docs explicitly show registration needs no review; "unknown" = the fetched docs do not establish it. wait_basis: "documented" if the wait is stated in the fetched docs, "estimate" if reasoned from partial info, "unknown" if not established, "none" when no wait applies. Never invent a waiting time and never label an app universally approval-free when that holds only for a particular scenario.
  "summary": string (2-3 sentences a developer can act on),
  "steps": [{{"title": string, "detail": string, "source": string (optional; MUST be one of the fetched URLs above - the page this step comes from; omit if not directly documented)}}],
  "requirements": [string],
  "nuances": [string] (things that bite people: scope tiers, redirect URL rules, review prerequisites, video requirements),
  "common_blockers": [{{"problem": string, "solution": string (the documented fix; use "not covered in the fetched docs" when the docs name the problem without a fix)}}],
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
        ap["requirement"] = "unknown"
        ap["kind"] = "unknown"
        ap["wait"] = "unknown"
        ap["wait_basis"] = "unknown"
        guide.setdefault("uncertainties", []).append(
            "The fetched documentation did not establish whether an approval or review is required; "
            "absence of a mention in limited documentation is not evidence that none is required.")
    if not isinstance(ap.get("required"), bool):
        ap.setdefault("kind", "unknown")
        if ap.get("wait") in (None, "", "none"):
            ap["wait"] = "unknown"
        if ap.get("requirement") == "not-required":
            ap["requirement"] = "unknown"
    # enforce the enums regardless of what the model emitted
    if ap.get("requirement") not in ("required", "conditional", "optional", "not-required", "unknown"):
        ap["requirement"] = "unknown" if not isinstance(ap.get("required"), bool) else (
            "required" if ap["required"] else "not-required")
    if ap.get("wait_basis") not in ("documented", "estimate", "unknown", "none"):
        w = str(ap.get("wait") or "").lower()
        ap["wait_basis"] = "none" if w == "none" else ("unknown" if w in ("", "unknown") else "estimate")
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


def _name_matches_site(name: str, site: str) -> bool:
    """Guard against resolving a name-only query to an unrelated site.
    Accepts when a normalized query token appears in the site's registrable
    hostname (or vice versa), or is a close fuzzy match (typos/variants)."""
    import difflib
    host = _registrable(urlparse(site).netloc).split(".")[0]
    nq = re.sub(r"[^a-z0-9]", "", host.lower())
    if not nq:
        return False
    for tok in re.split(r"[^a-z0-9]+", name.lower()):
        t = re.sub(r"[^a-z0-9]", "", tok)
        if len(t) < 3:
            continue
        if t in nq or nq in t:
            return True
        if difflib.SequenceMatcher(None, t, nq).ratio() >= 0.6:
            return True
    return False


def investigate(query: str) -> dict:
    """Run the full pipeline. Returns a guide dict with provenance metadata."""
    started = time.time()
    query = query.strip()
    if not query:
        raise ValueError("empty query")
    is_url_input = True
    if re.match(r"^https?://", query) or re.match(r"^[\w-]+(\.[\w-]+)+$", query):
        site = _sanitize_site(query)
        name = urlparse(site).netloc.replace("www.", "")
    else:
        name = query
        site = ""
        for h in _search(f"{query} official website"):
            cand = _sanitize_site(h)
            if cand and _name_matches_site(query, cand):
                site = cand
                break
        if not site:
            raise RuntimeError(
                f"Could not identify an official website matching '{query}'. "
                "Search results did not include a site whose name corresponds to this app, "
                "so no research was attempted rather than risk documenting the wrong product. "
                "Try the app's exact website URL instead (e.g. https://example.com).")
        is_url_input = False

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
        official = bool(dom) and _registrable(h) == dom
        docish = bool(re.search(r"oauth|authori|authentic|/docs|/documentation|develop|/api|/reference", urlparse(u).path, re.I))
        if official and docish:
            return 0
        if official:
            return 1
        return 2
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
    setup_ish = sum(1 for t in fetched.values()
                    if len(re.findall(r"create|register|redirect|client secret|client id", t, re.I)) >= 5)
    if (sum(1 for t in fetched.values() if _oauth_mentions(t) >= 3) < 2 or setup_ish < 2) and site:
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
                        and re.search(r"oauth|authorization|authentication|getting-started|quickstart|concepts|tutorial", v, re.I)
                        and v not in fetched and v not in extra):
                    extra.append(v)
            if len(extra) >= 6:
                break
        for v in extra[:4]:
            try:
                t = _fetch_text(v)
            except TransientFetchError:
                continue
            if t and len(t) > 400:
                fetched[v] = t

    if fetched and not is_url_input:
        toks = [re.sub(r"[^a-z0-9]", "", t) for t in re.split(r"[^a-z0-9]+", name.lower())]
        toks = [t for t in toks if len(t) >= 3]
        corpus = re.sub(r"[^a-z0-9]", "", " ".join(fetched.values()).lower())
        if toks and not any(t in corpus for t in toks):
            raise RuntimeError(
                f"The documentation found for '{query}' does not mention this app name - "
                "the search likely resolved to the wrong product, so no guide was generated "
                "rather than guess. Try the app's exact website URL instead.")

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

    # Post-validation: evidence and step sources may only cite URLs we actually fetched.
    norm_steps = []
    for s in guide.get("steps") or []:
        if isinstance(s, dict):
            if s.get("source") and s["source"] not in fetched:
                s.pop("source", None)
            norm_steps.append(s)
    guide["steps"] = norm_steps
    norm_blockers = []
    for b in guide.get("common_blockers") or []:
        if isinstance(b, str):
            norm_blockers.append({"problem": b, "solution": ""})
        elif isinstance(b, dict) and b.get("problem"):
            norm_blockers.append(b)
    guide["common_blockers"] = norm_blockers
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
