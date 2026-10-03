"""Listing pages for sources that publish no feed (PLAN.md section 2.6, Stage 3).

Each site lays out its listing its own way, so each has a named parser here, as JSON APIs do
in json_api.py. A parser reads one server-rendered listing page and returns its items. When a
site changes its layout the parser finds nothing, and the source reads EMPTY in source health:
a redesign surfaces as a failing source rather than as a silent drop in items.

Parsed with the standard library: a listing needs links, text and a few attributes, not a
browser, and nothing here runs the page's scripts.
"""

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from worker.collectors.dates import parse_date
from worker.models import RawItem, SourceConfig

UTC = ZoneInfo("UTC")
SYDNEY = ZoneInfo("Australia/Sydney")
PACIFIC = ZoneInfo("America/Los_Angeles")

# Elements that never have children, so a start tag is the whole element.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Their text is code, not content.
_SKIP = frozenset({"script", "style", "template", "noscript"})


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def iter(self) -> Iterator["Node"]:
        """This node's descendants, document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: str | None = None, *, cls: str | None = None) -> list["Node"]:
        """Descendants with this tag and, if given, this class among their classes."""
        return [
            n
            for n in self.iter()
            if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
        ]

    def find(self, tag: str | None = None, *, cls: str | None = None) -> "Node | None":
        return next(iter(self.find_all(tag, cls=cls)), None)

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    walk(child)

        walk(self)
        return " ".join(" ".join(parts).split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        # Close back to the matching element; an end tag nothing opened is ignored, which is
        # how a browser recovers from the same mistake.
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack[-1].tag not in _SKIP:
            self._stack[-1].children.append(data)


def parse_html(body: bytes) -> Node:
    builder = _TreeBuilder()
    builder.feed(body.decode("utf-8", errors="replace"))
    builder.close()
    return builder.root


_DAY_FORMATS = ("%d %B %Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y")


def parse_day(text: str, tz: ZoneInfo) -> datetime | None:
    """A date printed without a time ("30 September 2026", "Sep 28, 2026"), as the start of
    that day where the publisher is. The start, not noon: a later guess could put a page that
    was read the same morning in the future."""
    text = " ".join(text.split())
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz).astimezone(UTC)
        except ValueError:
            continue
    return None


def _https_url(source: SourceConfig, href: str | None) -> str | None:
    """The link as an absolute https URL, or None. Every source link published must be https
    (schemas/event.schema.json), and a javascript: or data: link is not one at all."""
    if not href:
        return None
    url = urljoin(source.url, href.strip())
    parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else None


def _item(
    source: SourceConfig,
    url: str,
    title: str,
    published: datetime | None,
    summary: str | None,
    fetched_at: datetime,
) -> RawItem:
    canonical = f"{title}|{url}|{summary or ''}".encode()
    return RawItem(
        source_id=source.id,
        url=url,
        guid=url,
        title=title,
        raw_summary=summary or None,
        published=published,
        fetched_at=fetched_at,
        payload_hash=hashlib.sha256(canonical).hexdigest(),
    )


def asd_news(source: SourceConfig, root: Node) -> list[RawItem]:
    """asd.gov.au/news: a Drupal view, one <article class="node--type-news"> per item, with
    the date as <time datetime>."""
    now = datetime.now(UTC)
    items = []
    for article in root.find_all("article", cls="node--type-news"):
        link = next((a for a in article.find_all("a") if a.attrs.get("href")), None)
        url = _https_url(source, link.attrs["href"]) if link else None
        heading = article.find("h3") or article.find("h2")
        when = article.find("time")
        title = (heading.text() if heading else "") or (
            link.attrs.get("aria-label", "") if link else ""
        )
        if not url or not title:
            continue
        summary = article.find("div", cls="field-name-field-summary")
        items.append(
            _item(
                source,
                url,
                title,
                parse_date(when.attrs.get("datetime")) if when else None,
                summary.text() if summary else None,
                now,
            )
        )
    return items


_TITLE_CLASS = re.compile(r"title", re.IGNORECASE)


def anthropic_news(source: SourceConfig, root: Node) -> list[RawItem]:
    """anthropic.com/news: every card is a link holding a <time> with the day as text, and a
    heading or an element whose class names it the title. Featured cards link outside /news/
    (a model launch is its own page), so the link's path is not the test; the date is."""
    now = datetime.now(UTC)
    items: dict[str, RawItem] = {}
    for link in root.find_all("a"):
        url = _https_url(source, link.attrs.get("href"))
        when = link.find("time")
        published = parse_day(when.text(), PACIFIC) if when else None
        if not url or published is None:
            continue
        heading = next(
            (
                n
                for n in link.iter()
                if n.tag in ("h1", "h2", "h3", "h4")
                or any(_TITLE_CLASS.search(c) for c in n.classes)
            ),
            None,
        )
        title = heading.text() if heading else ""
        if not title or url in items:
            continue
        body = next((p for p in link.find_all("p")), None)
        items[url] = _item(source, url, title, published, body.text() if body else None, now)
    return list(items.values())


def oaic_media(source: SourceConfig, root: Node) -> list[RawItem]:
    """oaic.gov.au/news/media-centre: Funnelback search cards. The card's link goes through a
    click-tracking redirect, and the page itself is its `url` parameter. Only a page on OAIC's
    own site is taken from it: the parameter is not a link OAIC's page shows anyone."""
    now = datetime.now(UTC)
    host = urlparse(source.url).netloc
    items = []
    for card in root.find_all("div", cls="card-content"):
        link = card.find("a", cls="card-title")
        href = link.attrs.get("href", "") if link else ""
        target = parse_qs(urlparse(href).query).get("url", [None])[0]
        url = _https_url(source, target or href)
        title = link.text() if link else ""
        if not url or not title or urlparse(url).netloc != host:
            continue
        summary = card.find("p", cls="card-text")
        when = card.find("p", cls="date")
        items.append(
            _item(
                source,
                url,
                title,
                parse_day(when.text(), SYDNEY) if when else None,
                summary.text() if summary else None,
                now,
            )
        )
    return items


WEB_PAGE_PARSERS: dict[str, Callable[[SourceConfig, Node], list[RawItem]]] = {
    "asd_news": asd_news,
    "anthropic_news": anthropic_news,
    "oaic_media": oaic_media,
}


def parse_web_page(source: SourceConfig, body: bytes) -> list[RawItem]:
    """Parse a listing page with the parser the source names.

    Raises KeyError for a parser that is not registered (a configuration error), and lets a
    parser's own exceptions propagate (real bugs), as parse_json_api does.
    """
    if source.parser not in WEB_PAGE_PARSERS:
        raise KeyError(f"no web page parser named '{source.parser}'")
    if not body:
        return []
    return WEB_PAGE_PARSERS[source.parser](source, parse_html(body))
