"""Read a business's own site for the contact details OSM does not carry.

Across 2491 cached rows, OSM has a phone for 26% and an email for 13%. The
email is the column outreach actually runs on, and it is the one the map is
worst at - mappers record what they can see from the street.

But by the time a row has a website, from the map or from `webapp.enrich`, the
missing details are usually one page away. So this fetches the homepage, reads
the addresses, numbers and social links off it, and follows a `Kontakt` link
when the homepage alone comes up short.

Cheap, unlike the website search: one host per business and no search engine to
be polite to, so a pass runs concurrently and finishes in minutes rather than
the hour a search pass costs. Every outbound call is injected, so the tests
drive the whole decision path without touching the network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import unquote, urljoin, urlsplit

from osm_businesses import Row, normalize_phone, normalize_website

from webapp.enrich import domain_core, social_kind
from webapp.results import FOUND_STRONG

logger = logging.getLogger(__name__)

Fetch = Callable[[str], str]
"""Takes a URL, returns the page's HTML, or "" when nothing usable came back.

Raises `SiteUnreachable` - and only then - when nothing answered at all.
"""


class SiteUnreachable(Exception):
    """Nothing answered at this address: DNS, refused, or timed out.

    Deliberately narrow. A 403 from a bot filter, a redirect to a parked page or
    an unreadable content type all mean "we learned nothing", which is a fact
    about the fetch. This means "there is no server there", which is a fact about
    the business - and the only one that earns the `dead` label.
    """


#: Values for ContactHit.status. Empty means the site was never read, which is
#: different from CONTACT_NONE: read, and it carried nothing.
CONTACT_OK = "ok"
CONTACT_NONE = "none"
CONTACT_DEAD = "dead"

#: Pages fetched per business: the homepage plus at most two `Kontakt` links.
#: A site that hides its address deeper than that is not worth the requests.
MAX_PAGES = 3

#: Anything past this is a page that is not going to have an address near the
#: top of it, and is more likely a file served with the wrong content type.
MAX_BYTES = 1_000_000
FETCH_TIMEOUT_S = 8.0

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: `mailto:` hrefs are read separately from the page text, because they arrive
#: percent-encoded and often carry a subject: `mailto:%20office@x.rs?subject=Upit`.
_MAILTO_RE = re.compile(r"mailto:([^\"'>\s]+)", re.IGNORECASE)

#: An address is exactly this and nothing either side of it.
_EMAIL_EXACT_RE = re.compile(_EMAIL_RE.pattern + r"$")

#: `<script>` and `<style>` bodies are stripped before anything is read out of a
#: page: an analytics blob is full of things shaped like addresses and numbers.
_NOISE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a\s*>",
                      re.IGNORECASE | re.DOTALL)

#: A `@` inside a filename: `logo@2x.png`, `hero@3x.webp`. The suffix is what
#: gives it away, since the rest of it is shaped exactly like an address.
_ASSET_SUFFIXES: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "css", "js",
     "woff", "woff2", "ttf", "eot", "mp4", "webm", "json", "map"}
)

#: Domains that appear on a business's site without being the business: the
#: platform it was built on, the newsletter tool, the error reporter, and the
#: placeholder somebody left in a contact form.
_NOT_THE_BUSINESS: frozenset[str] = frozenset(
    {
        "example", "yourdomain", "yoursite", "domain", "email", "mydomain",
        "sentry", "wixpress", "wix", "squarespace", "shopify", "godaddy",
        "wordpress", "automattic", "mailchimp", "sendgrid", "mailgun",
        "cloudflare", "google", "googlegroups", "googletagmanager", "gstatic",
        "facebook", "adobe", "jquery", "bootstrapcdn", "fontawesome",
        "w3", "schema", "sentry-cdn", "cdn",
    }
)

#: Free providers a small business legitimately publishes as its only address.
#: Ranked below its own domain but above anything unrecognised.
_FREE_PROVIDERS: frozenset[str] = frozenset(
    {"gmail", "yahoo", "hotmail", "outlook", "live", "icloud", "protonmail", "proton",
     "mts", "eunet", "sbb", "ptt", "open", "telekom", "yandex", "mail"}
)

#: `info@`, `office@` and friends: the address a business publishes for being
#: contacted, as opposed to one employee's.
_ROLE_LOCALS: frozenset[str] = frozenset(
    {"info", "office", "kontakt", "contact", "prodaja", "sales", "komercijala",
     "hello", "zdravo", "mail", "posta", "uprava", "marketing", "podrska", "support"}
)

#: Link text or href that means "our details are through here".
_CONTACT_HINT_RE = re.compile(
    r"kontakt|contact|o[-_ ]?nama|about|impressum|informacije|pisite|reach[-_ ]?us",
    re.IGNORECASE,
)

#: A Serbian number as a site would print it: an international prefix or a
#: leading zero, then 8-9 digits with any mix of spaces, dashes and slashes.
#: The prefix requirement is what keeps a PIB, a price and a date out.
_PHONE_RE = re.compile(r"(?<![\d/.\-])(\+381|00381|0)[\s/.\-]?(\d[\s/.\-]?){7,9}\d(?![\d.\-])")
_TEL_RE = re.compile(r"href\s*=\s*[\"']tel:([^\"']+)[\"']", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ContactHit:
    """What reading one business's site turned up."""

    email: str = ""
    phone: str = ""
    facebook: str = ""
    instagram: str = ""
    status: str = CONTACT_NONE
    """CONTACT_OK, CONTACT_NONE or CONTACT_DEAD. Never "" - that means unread."""
    source: str = ""
    """The page it came off, so a doubtful address can be checked."""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "phone": self.phone,
            "facebook": self.facebook,
            "instagram": self.instagram,
            "status": self.status,
            "source": self.source,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContactHit:
        return cls(
            email=str(data.get("email", "")),
            phone=str(data.get("phone", "")),
            facebook=str(data.get("facebook", "")),
            instagram=str(data.get("instagram", "")),
            status=str(data.get("status", CONTACT_NONE)),
            source=str(data.get("source", "")),
            checked_at=str(data.get("checked_at", "")),
        )

    @property
    def anything(self) -> bool:
        return bool(self.email or self.phone or self.facebook or self.instagram)


# ---------------------------------------------------------------------------
# Pure extraction
# ---------------------------------------------------------------------------


def strip_noise(html: str) -> str:
    """Drop script and style bodies, which are where the false positives live."""
    return _NOISE_RE.sub(" ", html)


def _is_asset(candidate: str) -> bool:
    return candidate.rsplit(".", 1)[-1].lower() in _ASSET_SUFFIXES


def _clean_email(candidate: str) -> str:
    """Trim what a page wraps around an address, and reject what is left if it is not one."""
    trimmed = unquote(candidate).strip().strip("<>()[]{}\"',;:").lower()
    return trimmed if _EMAIL_EXACT_RE.match(trimmed) else ""


def _mailto_addresses(html: str) -> list[str]:
    """Addresses out of `mailto:` hrefs, decoded and stripped of their query."""
    out: list[str] = []
    for match in _MAILTO_RE.finditer(html):
        target = unquote(match.group(1)).split("?")[0]
        out += [part for part in re.split(r"[,;]", target) if part.strip()]
    return out


def emails_in(html: str) -> list[str]:
    """Every plausible address on a page, lowercased, in the order they appear.

    `mailto:` links first, since they are the ones the business meant to publish
    and the plain-text pass would read them percent-encoded.
    """
    raw = _mailto_addresses(html)
    raw += [match.group(0) for match in _EMAIL_RE.finditer(strip_noise(html))]
    found: list[str] = []
    for item in raw:
        candidate = _clean_email(item)
        if not candidate or _is_asset(candidate):
            continue
        if domain_core(f"http://{candidate.split('@')[-1]}") in _NOT_THE_BUSINESS:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def pick_email(candidates: Sequence[str], site_url: str) -> str:
    """The one address most likely to reach this business.

    Ranked rather than filtered, because the alternatives are not equally bad:
    an address on the site's own domain is certainly theirs, a gmail address on
    their site is almost certainly theirs, and an address on some third party's
    domain is somebody else's however plausible it looks.
    """
    site = domain_core(site_url)

    def rank(address: str) -> tuple[int, int]:
        local, _, host = address.partition("@")
        core = domain_core(f"http://{host}")
        if core and core == site:
            tier = 0
        elif core in _FREE_PROVIDERS:
            tier = 1
        else:
            tier = 2
        role = 0 if local.split(".")[0] in _ROLE_LOCALS else 1
        return (tier, role)

    usable = [address for address in candidates if rank(address)[0] < 2]
    return min(usable, key=rank) if usable else ""


def phones_in(html: str) -> list[str]:
    """Every dialable number on a page, normalized the way `Row.phone` is."""
    text = _TAG_RE.sub(" ", strip_noise(html))
    raw = [match.group(1) for match in _TEL_RE.finditer(html)]
    raw += [match.group(0) for match in _PHONE_RE.finditer(text)]
    found: list[str] = []
    for candidate in raw:
        number = normalize_phone(candidate)
        digits = number.lstrip("+")
        # Serbia: 8-9 digits after a leading 0, or the same behind 381.
        if number.startswith("+381") or digits.startswith("381"):
            ok = 11 <= len(digits) <= 12
        else:
            ok = number.startswith("0") and 9 <= len(digits) <= 10
        if ok and number not in found:
            found.append(number)
    return found


def links_in(html: str, base_url: str) -> list[tuple[str, str]]:
    """`(absolute url, link text)` for every anchor with an href."""
    out: list[tuple[str, str]] = []
    for match in _LINK_RE.finditer(html):
        href = match.group(1).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = _TAG_RE.sub(" ", match.group(2)).strip()
        out.append((urljoin(base_url, href), text))
    return out


def contact_page_links(html: str, base_url: str) -> list[str]:
    """Links that look like they lead to the business's details.

    Restricted to the site's own host on purpose: following an off-site
    `Kontakt` is how a scraper wanders into a directory and comes back with the
    directory's address.
    """
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    found: list[str] = []
    for url, text in links_in(html, base_url):
        if urlsplit(url).netloc.lower().removeprefix("www.") != host:
            continue
        if not (_CONTACT_HINT_RE.search(url) or _CONTACT_HINT_RE.search(text)):
            continue
        if url.rstrip("/") != base_url.rstrip("/") and url not in found:
            found.append(url)
    return found


def socials_in(html: str, base_url: str) -> dict[str, str]:
    """The business's own Facebook and Instagram, as linked from its own site.

    No name matching needed here, unlike in the website search: a link on the
    business's own page is the business's by construction.
    """
    found = {"facebook": "", "instagram": ""}
    for url, _ in links_in(html, base_url):
        kind = social_kind(url)
        if kind and not found[kind]:
            found[kind] = url
    return found


def site_of(row: Row) -> str:
    """The page to read for this business, or "" when there is nothing to read.

    A `weak` guess from the website search is deliberately not read: attaching a
    stranger's email to a lead is worse than leaving the column empty.
    """
    if row.website:
        return row.website
    if row.found_confidence == FOUND_STRONG and row.found_website:
        return row.found_website
    return ""


def needs_contacts(row: Row) -> bool:
    """Whether reading this business's site could still tell the user anything."""
    if not site_of(row) or row.contact_status:
        return False
    return not (row.email and row.phone)


# ---------------------------------------------------------------------------
# The scraper
# ---------------------------------------------------------------------------


class ContactScraper:
    """Reads one business's site, shallowly."""

    def __init__(self, *, fetch: Fetch, max_pages: int = MAX_PAGES) -> None:
        self._fetch = fetch
        self._max_pages = max_pages

    def read(self, row: Row) -> ContactHit:
        start = normalize_website(site_of(row))
        if not start:
            return ContactHit(status=CONTACT_NONE, checked_at=_now())

        emails: list[str] = []
        phones: list[str] = []
        socials = {"facebook": "", "instagram": ""}
        source = ""
        queue = [start]
        seen: set[str] = set()
        unreachable = False

        while queue and len(seen) < self._max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            html, reached = self._page(url)
            if url == start and not reached:
                # Only the address the business publishes decides this. A
                # `Kontakt` link that 404s says nothing about the site.
                unreachable = True
            if not html:
                continue

            here = emails_in(html)
            if here and not emails:
                source = url
            emails += [address for address in here if address not in emails]
            phones += [number for number in phones_in(html) if number not in phones]
            for kind, link in socials_in(html, url).items():
                socials[kind] = socials[kind] or link

            email = pick_email(emails, start)
            if email and (phones or row.phone):
                # Everything worth having: no reason to open another page.
                break
            queue += [link for link in contact_page_links(html, url) if link not in seen]

        if unreachable:
            # Nothing answered at this address at all. Worth knowing on its own:
            # a business whose site is gone is a lead again, not one to skip.
            # Anything short of that - a bot filter, an unreadable page - is a
            # failed read, not a dead business, and must not be reported as one.
            return ContactHit(status=CONTACT_DEAD, checked_at=_now())

        hit = ContactHit(
            email=pick_email(emails, start),
            phone=phones[0] if phones else "",
            facebook=socials["facebook"],
            instagram=socials["instagram"],
            source=source,
            checked_at=_now(),
        )
        return replace(hit, status=CONTACT_OK if hit.anything else CONTACT_NONE)

    def _page(self, url: str) -> tuple[str, bool]:
        """`(html, something answered)`. The flag is what separates dead from unreadable."""
        try:
            return (self._fetch(url) or "", True)
        except SiteUnreachable:
            logger.debug("nothing answered at %s", url)
            return ("", False)
        except Exception:  # noqa: BLE001 - one broken site is one row's answer
            logger.debug("could not read %s", url, exc_info=True)
            return ("", False)


# ---------------------------------------------------------------------------
# The real fetch
# ---------------------------------------------------------------------------


def http_fetch(session: Any, user_agent: str) -> Fetch:
    """Build the page fetcher: one GET, capped, HTML only.

    Capped because a link labelled `Kontakt` occasionally serves a PDF or a
    video, and neither has an address in the first kilobyte.
    """

    def once(url: str) -> str:
        response = session.get(
            url,
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
            stream=True,
        )
        try:
            if response.status_code >= 400:
                # The server is there and said no - a bot filter, a login wall,
                # a moved page. Nothing learned, but the site is alive.
                return ""
            kind = response.headers.get("Content-Type", "")
            if kind and "html" not in kind.lower() and "text" not in kind.lower():
                return ""
            body = response.raw.read(MAX_BYTES, decode_content=True) or b""
        finally:
            response.close()
        return body.decode(response.encoding or "utf-8", errors="replace")

    def fetch(url: str) -> str:
        try:
            return once(url)
        except Exception as exc:  # noqa: BLE001 - retried over http, then reported
            if not url.lower().startswith("https://"):
                raise SiteUnreachable(str(exc)) from exc
        # Plenty of small Serbian sites are still http-only, and OSM records
        # them - or `normalize_website` guesses them - as https.
        try:
            return once("http://" + url[len("https://") :])
        except Exception as exc:  # noqa: BLE001 - now it really is unreachable
            raise SiteUnreachable(str(exc)) from exc

    return fetch


# ---------------------------------------------------------------------------
# Applying what was read back onto the rows
# ---------------------------------------------------------------------------


def apply_contact_hits(rows: list[Row], hits: dict[int, ContactHit]) -> list[Row]:
    """Overlay the scraped details onto the rows, keyed by OSM id.

    Same bargain as `apply_site_hits`: the OSM columns win where they exist, and
    what was read off the web lands in `found_email` / `found_phone` so the
    table can say where it came from. The socials are the exception - OSM has
    them for 6% of rows, and a link from the business's own homepage is as
    authoritative as a mapper's.
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
                found_email=hit.email,
                found_phone=hit.phone,
                contact_status=hit.status,
                facebook=row.facebook or hit.facebook,
                instagram=row.instagram or hit.instagram,
            )
        )
    return filled


def pending_contacts(rows: Iterable[Row], hits: dict[int, ContactHit]) -> list[Row]:
    """Rows a pass could still learn something about."""
    return [row for row in rows if row.osm_id not in hits and needs_contacts(row)]
