"""Find the website a business has but never put on the map.

OSM's `website` tag is filled in by whoever mapped the shop, which is usually
not the shop. A business with a perfectly good site therefore lands in the "no
website" pile, and calling it is wasted time. This module takes such a row,
searches the web for it, and decides whether any of the results is the
business's own site.

The hard part is not searching, it is *rejecting*. A search for a Serbian
business returns catalogues (companywall, navidiku, planplus), map mirrors and
review sites long before it returns the business. So the pipeline is:

    query -> results -> drop known directories -> keep domains built from the
    name -> confirm the domain actually answers -> confidence

Every outbound call is injected, so the tests run the whole decision path
without touching the network.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

from osm_businesses import Row, fold_text, normalize_website

from webapp.results import FOUND_NONE, FOUND_STRONG, FOUND_WEAK

logger = logging.getLogger(__name__)


class SearchFailed(RuntimeError):
    """The search engine could not be reached. Says nothing about the business."""


Search = Callable[[str], list[dict[str, Any]]]
"""Takes a query, returns dicts with `href`, `title` and `body`."""

Alive = Callable[[str], str]
"""Takes a URL, returns the URL it settled on, or "" if nothing answered."""

#: Seconds between searches. DuckDuckGo answers a steady trickle indefinitely
#: and blocks a burst, and a blocked run is worth less than a slow one.
SEARCH_INTERVAL_S = 2.0

#: How many results to look at per business. Past the first handful it is all
#: catalogue, and each result costs nothing but is one more chance at a false
#: positive.
MAX_RESULTS = 8

SEARCH_REGION = "rs-sr"

#: Which engines to ask. Breadth is the point, not preference: any single engine
#: rate-limits within a handful of queries and then answers "no results", which
#: is indistinguishable from a real miss. `ddgs` falls through the list until one
#: answers, so six engines is what makes a long run possible at all.
#:
#: The encyclopedias `ddgs` would otherwise include under "auto" (wikipedia,
#: grokipedia) are left out: they answer a different question and never carry a
#: small business's own domain.
SEARCH_BACKEND = os.environ.get(
    "OSM_SEARCH_BACKEND", "duckduckgo,brave,google,yahoo,startpage,mojeek"
)

#: A search that errors is retried once: the engines fail transiently often
#: enough that one timeout is not evidence of anything.
SEARCH_ATTEMPTS = 2

#: Never a business's own site: business directories, catalogues, map mirrors,
#: review sites, marketplaces and the rest of the layer that sits between a
#: search and the business. Matched on the registrable domain, so
#: `www.companywall.rs` and `companywall.rs/firma/...` both land here.
AGGREGATORS: frozenset[str] = frozenset(
    {
        # Serbian and regional business catalogues
        "companywall", "navidiku", "planplus", "feruvi", "011info", "bizniskatalog",
        "zutestrane", "goldenpages", "adresar", "firme", "kompanije", "poslovni",
        "privrednik", "srbijabiz", "biznisregistar", "infobiz", "telefonskiimenik",
        "yellowpages", "yell", "cylex", "opendi", "tuugo", "infoisinfo", "hotfrog",
        "europages", "kompass", "wlw", "bisnode", "solidnost", "bonitet",
        "apr", "nbs", "paragraf", "mfin",
        # maps, reviews, listings
        "google", "goo", "bing", "yandex", "waze", "here", "mapy", "openstreetmap",
        "tripadvisor", "yelp", "foursquare", "swarmapp", "zomato", "trustpilot",
        "booking", "airbnb", "expedia", "hotels", "agoda", "trivago", "kayak",
        "restaurantguru", "menu", "wolt", "glovoapp", "glovo", "donesi", "mrdstavi",
        # social and video
        "facebook", "fb", "instagram", "twitter", "x", "tiktok", "youtube", "youtu",
        "linkedin", "pinterest", "vk", "telegram", "wa", "whatsapp", "viber",
        # marketplaces and classifieds
        "kupujemprodajem", "olx", "halooglasi", "nekretnine", "cenoteka", "idealo",
        "amazon", "ebay", "aliexpress", "temu", "shopmania", "pricerunner",
        # generic infrastructure that shows up as a "site"
        "wikipedia", "wikidata", "wikimedia", "blogspot", "wordpress", "wixsite",
        "weebly", "webnode", "jimdo", "sites", "linktr", "bit", "tinyurl",
        "archive", "web", "scribd", "issuu", "slideshare", "medium",
        "findglocal", "bestofserbia", "sviznaju", "gdejesta", "mojafirma",
        "nadjifirmu", "poslovnipretraga", "biznisinfo", "katalogfirmi",
    }
)

#: Domains whose presence is worth recording as a social profile even though
#: they can never be the answer to "does this business have a website".
SOCIAL_DOMAINS: dict[str, str] = {
    "facebook": "facebook",
    "fb": "facebook",
    "instagram": "instagram",
}

#: Words that say what a business *does*, not which business it is. Stripped
#: before matching, so `Pekara Trpkovic` is looked for as `trpkovic` and
#: `pekara.rs` does not count as a match for every bakery in the country.
GENERIC_WORDS: frozenset[str] = frozenset(
    {
        # trades
        "pekara", "pekare", "apoteka", "apoteke", "market", "supermarket", "minimarket",
        "prodavnica", "radnja", "restoran", "kafana", "kafe", "kafic", "cafe", "coffee",
        "bar", "picerija", "pizzeria", "pizza", "poslasticarnica", "mesara", "ribarnica",
        "butik", "salon", "frizerski", "frizer", "kozmeticki", "kozmeticar", "berbernica",
        "auto", "autoservis", "servis", "vulkanizer", "autoperionica", "perionica",
        "stomatoloska", "stomatolog", "zubar", "ordinacija", "ambulanta", "laboratorija",
        "veterinarska", "veterinar", "optika", "opticar", "knjizara", "papirnica",
        "cvecara", "cvecarnica", "pijaca", "trafika", "kladionica", "teretana", "fitnes",
        "hotel", "motel", "hostel", "apartmani", "pansion", "prenociste", "turisticka",
        "agencija", "advokat", "advokatska", "kancelarija", "biro", "studio", "atelje",
        "gradjevinska", "stolarija", "bravarija", "limarija", "elektro", "vodoinstalater",
        "shop", "store", "boutique", "salon", "centar", "center", "centre",
        # legal forms and shopfront noise
        "doo", "d.o.o", "ad", "a.d", "pr", "sztr", "str", "sur", "szr", "sztur", "ur",
        "zr", "dooel", "kd", "od", "preduzece", "firma", "company", "co", "ltd", "llc",
        "gmbh", "sp", "z.o.o",
    }
)

#: A bare domain quoted inside a result snippet: "zvanicni sajt trpkovic.rs".
#: Deliberately narrow on the suffix - a wide pattern matches file names,
#: version numbers and abbreviations.
_MENTION_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:rs|com|net|org|info|biz|shop|online|store|site|eu|co\.rs|org\.rs))\b",
    re.IGNORECASE,
)

#: Suffixes stripped from a host to get at the label that identifies the owner.
#: Longest first, so `co.rs` wins over `rs`.
_PUBLIC_SUFFIXES: tuple[str, ...] = (
    "co.rs", "org.rs", "edu.rs", "ac.rs", "gov.rs", "in.rs", "co.uk", "com.hr",
    "rs", "com", "net", "org", "info", "biz", "shop", "online", "store", "site",
    "eu", "ba", "hr", "me", "mk", "si", "bg", "de", "at", "ch", "it", "uk",
)

#: Subdomains that are decoration, not identity.
_NOISE_LABELS: frozenset[str] = frozenset({"www", "www2", "web", "shop", "sajt", "m", "en", "sr"})

#: How close a domain has to be to the name before it counts without being equal.
_STRONG_RATIO = 0.85

#: Tokens shorter than this match too much to mean anything on their own.
_MIN_TOKEN = 4

#: Country TLDs that are somebody else's country. A name match on one of these
#: is a different business with the same name until proven otherwise.
_FOREIGN_TLDS: frozenset[str] = frozenset(
    {"ba", "hr", "si", "me", "mk", "bg", "al", "gr", "ro", "hu", "de", "at",
     "ch", "it", "fr", "es", "pl", "cz", "sk", "ru", "ua", "tr", "uk"}
)

#: First path segments on a social host that are never a business's own profile.
_SOCIAL_JUNK_PATHS: frozenset[str] = frozenset(
    {"popular", "explore", "p", "reel", "reels", "hashtag", "groups", "pages",
     "marketplace", "events", "watch", "story", "stories", "search", "tv", "accounts"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SiteHit:
    """What the search concluded about one business."""

    website: str = ""
    confidence: str = FOUND_NONE
    """FOUND_STRONG, FOUND_WEAK or FOUND_NONE. Never "" - that means unchecked."""
    source: str = ""
    """"search" (a result linked it), "mention" (a snippet quoted it) or "social"."""
    facebook: str = ""
    instagram: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "website": self.website,
            "confidence": self.confidence,
            "source": self.source,
            "facebook": self.facebook,
            "instagram": self.instagram,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteHit:
        return cls(
            website=str(data.get("website", "")),
            confidence=str(data.get("confidence", FOUND_NONE)),
            source=str(data.get("source", "")),
            facebook=str(data.get("facebook", "")),
            instagram=str(data.get("instagram", "")),
            checked_at=str(data.get("checked_at", "")),
        )


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def domain_core(url: str) -> str:
    """The label that identifies who owns a URL: `https://www.trpkovic.co.rs/x` -> `trpkovic`.

    Returns "" for anything that is not a host, so a caller can treat "not a
    URL" and "not a business domain" the same way.
    """
    candidate = url.strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower().strip(".")
    if not host or "." not in host or " " in host:
        return ""
    labels = host.split(".")
    for suffix in _PUBLIC_SUFFIXES:
        parts = suffix.split(".")
        if labels[-len(parts) :] == parts and len(labels) > len(parts):
            labels = labels[: -len(parts)]
            break
    else:
        labels = labels[:-1] if len(labels) > 1 else labels
    while len(labels) > 1 and labels[0] in _NOISE_LABELS:
        labels = labels[1:]
    return labels[-1] if labels else ""


def is_aggregator(url: str) -> bool:
    core = domain_core(url)
    return bool(core) and core in AGGREGATORS


def social_kind(url: str) -> str:
    """"facebook", "instagram" or "" - which social network a URL belongs to."""
    return SOCIAL_DOMAINS.get(domain_core(url), "")


def distinctive_tokens(name: str) -> list[str]:
    """The parts of a name that identify *this* business rather than its trade.

    An empty list means the name says nothing a search could match on - `Pekara`
    or `Apoteka` - and such a row must never be searched: whatever came back
    would belong to some other bakery.
    """
    folded = fold_text(name)
    words = re.findall(r"[a-z0-9]+", folded)
    kept = [word for word in words if word not in GENERIC_WORDS and len(word) >= 2]
    # An all-generic name still has no identity, but a name that is generic
    # *plus* a number ("Apoteka 5") has none either - the number is a branch.
    kept = [word for word in kept if not word.isdigit()]
    return kept


def search_query(row: Row) -> str:
    """What to ask the search engine.

    The name is quoted so the engine does not wander off into synonyms, and the
    place and street are added unquoted as the soft hints they are - a business
    that moved should still be found.
    """
    parts = [f'"{row.name.strip()}"']
    for extra in (row.place, row.street):
        if extra.strip():
            parts.append(extra.strip())
    return " ".join(parts)


def _match_strength(tokens: Sequence[str], core: str) -> str | None:
    """How well a domain matches a name: FOUND_STRONG, FOUND_WEAK or None."""
    if not tokens or not core:
        return None
    stripped = core.replace("-", "")
    joined = "".join(tokens)
    if stripped == joined:
        return FOUND_STRONG
    # The whole name inside a longer domain - `trpkovic` in `pekaratrpkovic`.
    # Only this direction: a domain that is a *fragment* of the name covers part
    # of it, which is the partial case the token count below judges.
    if len(joined) >= _MIN_TOKEN and joined in stripped:
        return FOUND_STRONG
    if SequenceMatcher(None, joined, stripped).ratio() >= _STRONG_RATIO:
        return FOUND_STRONG
    present = [token for token in tokens if len(token) >= _MIN_TOKEN and token in stripped]
    if not present:
        return None
    # Some of the name is in the domain but not all of it: right family, maybe
    # the wrong business. Worth eyeballing, not worth acting on unseen.
    return FOUND_STRONG if len(present) == len(tokens) else FOUND_WEAK


def _tld_adjusted(strength: str, url: str) -> str:
    """Weaken a match that is a perfect name on the wrong country's TLD.

    `Restoran Zlatnik` in Nis matching `restoranzlatnik.ba` is a real Bosnian
    restaurant with the same name, not this one. The name alone cannot tell them
    apart, so the domain's country does.
    """
    if strength != FOUND_STRONG:
        return strength
    host = urlsplit(url if "://" in url else f"http://{url}").netloc.lower()
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    return FOUND_WEAK if tld in _FOREIGN_TLDS else strength


def _title_confirms(tokens: Sequence[str], text: str) -> bool:
    folded = fold_text(text)
    return bool(tokens) and all(token in folded for token in tokens)


def _mentioned_domains(results: Iterable[dict[str, Any]]) -> list[str]:
    """Domains quoted in the snippets rather than linked.

    This is the case the catalogues create: they outrank the business itself,
    but their entry for it prints its address in plain text.
    """
    found: list[str] = []
    for item in results:
        text = f"{item.get('title', '')} {item.get('body', '')}"
        for match in _MENTION_RE.finditer(text):
            domain = match.group(1).lower()
            if domain not in found:
                found.append(domain)
    return found


# ---------------------------------------------------------------------------
# The finder
# ---------------------------------------------------------------------------


class WebsiteFinder:
    """Decides whether one business has a site the map does not know about."""

    def __init__(
        self,
        *,
        search: Search,
        alive: Alive,
        rate_limiter: Any | None = None,
        max_results: int = MAX_RESULTS,
    ) -> None:
        self._search = search
        self._alive = alive
        self._rate_limiter = rate_limiter
        self._max_results = max_results

    def find(self, row: Row) -> SiteHit:
        tokens = distinctive_tokens(row.name)
        if not tokens:
            return SiteHit(confidence=FOUND_NONE, checked_at=_now())

        try:
            if self._rate_limiter is not None:
                self._rate_limiter.wait()
            results = list(self._search(search_query(row)))[: self._max_results]
        except Exception as exc:  # noqa: BLE001 - one business must not fail the run
            # An empty confidence means *unchecked*, which is the truth here and
            # is deliberately different from FOUND_NONE. Caching a timeout as
            # "this business has no site" would retire a real lead for good.
            logger.warning("website search failed for %r: %s", row.name, exc)
            return SiteHit(confidence="", checked_at=_now())

        socials = self._socials(results, tokens)
        hit = self._from_links(results, tokens) or self._from_mentions(results, tokens)
        if hit is None:
            confidence = FOUND_WEAK if (socials["facebook"] or socials["instagram"]) else FOUND_NONE
            source = "social" if confidence == FOUND_WEAK else ""
            return SiteHit(confidence=confidence, source=source, checked_at=_now(), **socials)

        website, confidence, source = hit
        settled = self._alive(website)
        if not settled:
            # The name matched but nothing answers: an expired domain or a typo
            # squatter. Reporting it would send the user to a dead page.
            confidence = FOUND_WEAK if (socials["facebook"] or socials["instagram"]) else FOUND_NONE
            source = "social" if confidence == FOUND_WEAK else ""
            return SiteHit(confidence=confidence, source=source, checked_at=_now(), **socials)

        return SiteHit(
            website=settled, confidence=confidence, source=source, checked_at=_now(), **socials
        )

    def _from_links(
        self, results: Sequence[dict[str, Any]], tokens: Sequence[str]
    ) -> tuple[str, str, str] | None:
        """The best of the results that actually link somewhere plausible."""
        best: tuple[str, str, str] | None = None
        for item in results:
            url = str(item.get("href", "")).strip()
            if not url or is_aggregator(url):
                continue
            strength = _match_strength(tokens, domain_core(url))
            if strength is None:
                # Deliberately no fallback on the title here. Every directory
                # entry is titled with the business's name - trusting that is
                # what puts `bestofserbia.rs/kompanije/...` in the results as if
                # it were the shop's own site. The domain has to carry the name.
                continue
            strength = _tld_adjusted(strength, url)
            if strength == FOUND_STRONG:
                return (url, FOUND_STRONG, "search")
            if best is None:
                best = (url, strength, "search")
        return best

    def _from_mentions(
        self, results: Sequence[dict[str, Any]], tokens: Sequence[str]
    ) -> tuple[str, str, str] | None:
        """A domain printed in a snippet. Only an exact name match is trusted here.

        The context is somebody else's page, so there is nothing to corroborate
        it with: `trpkovic.rs` inside an entry titled `Pekara Trpkovic` is the
        business, `nekamreza.rs` in the same paragraph is the catalogue's own
        footer.
        """
        for domain in _mentioned_domains(results):
            if is_aggregator(domain):
                continue
            if _match_strength(tokens, domain_core(domain)) == FOUND_STRONG:
                return (normalize_website(domain), FOUND_STRONG, "mention")
        return None

    def _socials(self, results: Sequence[dict[str, Any]], tokens: Sequence[str]) -> dict[str, str]:
        """Facebook and Instagram profiles that look like they belong to this business.

        A dead site is still a lead, and for a lot of small Serbian businesses
        the Facebook page *is* the website.
        """
        found = {"facebook": "", "instagram": ""}
        for item in results:
            url = str(item.get("href", "")).strip()
            kind = social_kind(url)
            if not kind or found[kind]:
                continue
            segments = [part for part in urlsplit(url).path.split("/") if part]
            if not segments or segments[0].lower() in _SOCIAL_JUNK_PATHS:
                continue
            handle = fold_text(segments[0]).replace(".", "").replace("_", "")
            if _match_strength(tokens, handle) or _title_confirms(tokens, str(item.get("title", ""))):
                found[kind] = url
        return found


# ---------------------------------------------------------------------------
# Applying what was found back onto the rows
# ---------------------------------------------------------------------------


def apply_site_hits(rows: list[Row], hits: dict[int, SiteHit]) -> list[Row]:
    """Overlay the search's findings onto the rows, keyed by OSM id.

    The OSM `website`, `facebook` and `instagram` tags win where they exist:
    what a mapper wrote down beats what a search guessed. The finding lands in
    the separate `found_*` fields so the table can say where it came from and
    the user can judge a weak one.
    """
    if not hits:
        return rows
    filled: list[Row] = []
    for row in rows:
        hit = hits.get(row.osm_id)
        if hit is None:
            filled.append(row)
            continue
        filled.append(
            replace(
                row,
                found_website=hit.website,
                found_confidence=hit.confidence,
                found_source=hit.source,
                facebook=row.facebook or hit.facebook,
                instagram=row.instagram or hit.instagram,
            )
        )
    return filled


def needs_check(row: Row) -> bool:
    """Whether searching for this row could tell the user anything new.

    A row that already carries a site needs nothing, a row already checked is
    cached, and a row whose name identifies nothing cannot be searched at all.
    """
    return not row.website and not row.found_confidence and bool(distinctive_tokens(row.name))


# ---------------------------------------------------------------------------
# The real outbound calls
# ---------------------------------------------------------------------------

#: How long to wait for a domain to prove it is alive. Short on purpose: this
#: runs once per business and a slow host is not worth a whole run's time.
ALIVE_TIMEOUT_S = 6.0

#: Status codes that mean "there is a site here" even though they are not 200.
#: 401/403 are hosts that dislike a scripted client, not empty addresses.
_ALIVE_ANYWAY: frozenset[int] = frozenset({401, 403, 405, 406, 429})


def ddg_search(
    query: str, *, region: str = SEARCH_REGION, max_results: int = MAX_RESULTS
) -> list[dict[str, Any]]:
    """One web search, retried once. Imported lazily so `ddgs` is not needed to import this module."""
    from ddgs import DDGS

    last: Exception | None = None
    for attempt in range(SEARCH_ATTEMPTS):
        try:
            return DDGS().text(
                query,
                region=region,
                max_results=max_results,
                safesearch="off",
                backend=SEARCH_BACKEND,
            )
        except Exception as exc:  # noqa: BLE001 - retried, then reported to the caller
            last = exc
            logger.debug("search attempt %d failed for %r: %s", attempt + 1, query, exc)
    raise SearchFailed(str(last)) from last


def http_alive(session: Any, user_agent: str) -> Alive:
    """Build the liveness check: does this domain actually serve anything?

    A domain built from the business's name is the best signal there is, and
    also the easiest to squat, mistype or let expire. Confirming it answers
    turns "a domain exists with this name" into "this business has a site".

    HEAD first because it is free, GET as the fallback: plenty of Serbian hosts
    answer HEAD with 405 and serve the page perfectly well on GET.
    """

    def alive(url: str) -> str:
        target = normalize_website(url)
        if not target:
            return ""
        for method in ("head", "get"):
            try:
                response = session.request(
                    method,
                    target,
                    timeout=ALIVE_TIMEOUT_S,
                    allow_redirects=True,
                    headers={"User-Agent": user_agent},
                    stream=(method == "get"),
                )
            except Exception:  # noqa: BLE001 - unreachable is an answer, not an error
                continue
            status = getattr(response, "status_code", 0)
            if status < 400 or status in _ALIVE_ANYWAY:
                settled = str(getattr(response, "url", "") or target)
                response.close()
                return settled
            response.close()
        return ""

    return alive
