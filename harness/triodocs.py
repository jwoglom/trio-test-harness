"""`th docs`: search and read the app's documentation, as plain text: Trio's at https://triodocs.org, or with
--app loop LoopDocs at https://loopkit.github.io/loopdocs/ (both MkDocs sites).

    th docs search "maximum bolus"          # best-matching sections: title, page, link, snippet
    th docs toc                             # every page, as a tree by URL
    th docs page settings/                  # a page as text (headings, lists, tables, admonitions, links)
    th docs page https://triodocs.org/configuration/new-user-setup/#onboarding-wizard   # one section

No browser: the site's own search index (search/search_index.json: every section's title and text), its sitemap
(sitemap.xml) and each page's HTML article, fetched over HTTPS and cached for a day under runs/.tmp/<site>/.
Use it to find where a setting or option lives in the app (which screen, what it is called, what it does) before
looking for it there. Paths are relative to the site (LoopDocs' operation/features/ is
https://loopkit.github.io/loopdocs/operation/features/).
"""
from __future__ import annotations

import gzip
import html
import http.client
import json
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .config import REPO_ROOT
from .util import HarnessError

BASE = "https://triodocs.org/"
CACHE = REPO_ROOT / "runs" / ".tmp" / "triodocs"
TTL = 24 * 3600


def _base() -> str:
    """The current app's docs site (harness/apps.py), ending in /."""
    from . import apps
    return apps.current().docs_url


def _cache() -> Path:
    base = _base()
    return CACHE if base == BASE else CACHE.parent / (urlparse(base).path.strip("/") or urlparse(base).netloc)


def _rel(url_path: str) -> str:
    """A URL path on the docs site, relative to the site (LoopDocs lives under /loopdocs/)."""
    prefix = urlparse(_base()).path
    return url_path[len(prefix):] if url_path.startswith(prefix) else url_path.lstrip("/")


def _get(path: str, *, ttl: int = TTL) -> str:
    """GET the site's path (or an absolute URL on the site), cached; a stale cache is used if the site is unreachable."""
    base = _base()
    url = urljoin(base, path)
    if urlparse(url).netloc != urlparse(base).netloc:
        raise HarnessError(f"{url}: only {base} pages")
    key = re.sub(r"[^\w.-]+", "_", _rel(urlparse(url).path).strip("/") or "index") + ".cache"
    cache = _cache()
    f = cache / key
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return f.read_text()
    try:
        text = fetch(url)
    except (OSError, http.client.HTTPException) as e:
        text = _fetch_through_daemon(url)
        if text is None:
            if f.exists():
                return f.read_text()
            code = getattr(e, "code", None)
            raise HarnessError(f"{url}: " + (f"HTTP {code}" if code else f"{type(e).__name__}: {e}")
                               + (" (no such page; see `th docs toc`)" if code == 404 else "")) from None
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return text


REMOTE = {"docs_fetch"}


def fetch(url: str, attempts: int = 64) -> str:
    """The page at url. Through the agent sandbox's network proxy, triodocs.org's 640 KB search index arrived cut short
    (IncompleteRead), so it is asked for gzipped first, and a plain body cut short is completed with Range requests
    (a range of a gzipped body need not line up with the first one's bytes)."""
    try:
        with _open(url, {"Accept-Encoding": "gzip"}) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
        return body.decode("utf-8", "replace")
    except http.client.IncompleteRead:
        pass
    body = b""
    for _ in range(attempts):
        try:
            with _open(url, {"Range": f"bytes={len(body)}-"} if body else {}) as r:
                if body and r.status != 206:
                    body = b""                         # the range was not honoured: this is the whole body again
                body += r.read()
            return body.decode("utf-8", "replace")
        except http.client.IncompleteRead as e:
            if not e.partial:
                break                                  # no progress: another try would fare no better
            body += e.partial
    raise http.client.IncompleteRead(body)


def _open(url: str, headers: dict):
    req = urllib.request.Request(url, headers={"User-Agent": "trio-test-harness docs", **headers})
    return urllib.request.urlopen(req, timeout=30)


def docs_fetch(target: str, url: str) -> str:
    """fetch() in the harness daemon (REMOTE), which runs outside the agent's sandbox."""
    if urlparse(url).netloc not in {urlparse(a.docs_url).netloc for a in _apps()}:
        raise HarnessError(f"{url}: not a docs site")
    return fetch(url)


def _apps():
    from . import apps
    return apps.APPS.values()


def _fetch_through_daemon(url: str) -> str | None:
    """The page fetched by a running harness daemon, None when none runs or it cannot either."""
    from . import daemon as Dm
    for target in ("sim", "rooted-vnc", "watch"):
        if Dm.is_running(target):
            try:
                return Dm.RemoteDriver(target, timeout=120)._call("docs_fetch", url)
            except HarnessError:
                return None
    return None


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def index() -> list[dict]:
    """The search index's sections: {"location", "title", "text"} (text as plain text), in site order."""
    docs = json.loads(_get("search/search_index.json"))["docs"]
    return [{"location": d["location"], "title": _strip(d.get("title", "")), "text": _strip(d.get("text", ""))}
            for d in docs]


def _page_titles(docs: list[dict]) -> dict[str, str]:
    """page location -> its title (index pages are titled "Index" in the index: use their first heading instead)."""
    titles: dict[str, str] = {}
    for d in docs:
        page, _, anchor = d["location"].partition("#")
        if not anchor and d["title"] not in ("Index", "") and page not in titles:
            titles[page] = d["title"]
        elif anchor and page not in titles:
            titles[page] = d["title"]
    return titles


def search(query: str, limit: int = 10) -> list[dict]:
    """Sections ranked by the query's words in their title (x10) and text, with a phrase bonus; each with its page's
    title, a link and a snippet around the first match."""
    words = [w for w in re.split(r"[\s\-]+", query.lower()) if w]
    if not words:
        raise HarnessError("empty search")
    docs = index()
    titles = _page_titles(docs)
    phrase = " ".join(words)
    hits = []
    for d in docs:
        title, text = d["title"].lower(), d["text"].lower()
        if not d["location"].partition("#")[2] and title in ("index", ""):
            continue                                   # a page's whole-text entry duplicates its sections
        score = sum(10 * title.count(w) + min(text.count(w), 5) for w in words)
        if not all(w in title or w in text for w in words):
            score //= 3                                # prefer sections that have every word
        if phrase in title:
            score += 40
        elif phrase in text:
            score += 15
        if score:
            hits.append((score, d))
    hits.sort(key=lambda h: -h[0])
    out = []
    for score, d in hits[:limit]:
        text = d["text"]
        low = text.lower()
        at = low.find(phrase) if phrase in low else min((low.find(w) for w in words if w in low), default=0)
        start = max(0, at - 80)
        snippet = ("…" if start else "") + text[start:start + 240] + ("…" if start + 240 < len(text) else "")
        page = d["location"].partition("#")[0]
        out.append({"title": d["title"], "page": titles.get(page, page or "Home"), "url": urljoin(_base(), d["location"]),
                    "snippet": snippet, "score": score})
    return out


def toc() -> list[tuple[int, str, str]]:
    """Every page as (depth, title, path), ordered as a tree by URL path (the site's navigation is pruned per page,
    so the sitemap is the complete list)."""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", _get("sitemap.xml"))
    titles = _page_titles(index())
    rows = []
    for u in locs:
        path = _rel(urlparse(u).path)
        depth = path.rstrip("/").count("/") + (1 if path else 0)
        rows.append((depth, titles.get(path, path.rstrip("/").rsplit("/", 1)[-1] or "Home"), path))
    rows.sort(key=lambda r: [p for p in r[2].rstrip("/").split("/")])
    return rows


class _Text(HTMLParser):
    """The page's <article> as readable text: markdown-ish headings, lists, tables, admonitions and links."""
    SKIP = {"script", "style", "svg", "button", "nav", "form", "noscript"}
    BLOCK = {"p", "div", "section", "br", "hr", "table", "tr", "ul", "ol", "dl", "pre", "blockquote", "details",
             "summary", "figure", "figcaption"}

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.url = page_url
        self.out: list[str] = []
        self.depth = 0             # inside <article>
        self.skip = 0
        self.lists: list[str] = []
        self.href: str | None = None
        self.link_text: list[str] = []
        self.cell = False
        self.adm_title = False     # inside an admonition's title <p> ("Tip", "Note", ...)
        self.tab_labels = 0        # labels seen inside a tab strip (0: not in one)
        self.anchors: dict[str, int] = {}   # heading id -> position in out

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "article" or (tag == "div" and "md-content__inner" in cls):
            self.depth += 1
            return
        if not self.depth:
            return
        if self.skip or tag in self.SKIP or "headerlink" in cls or "md-content__button" in cls:
            if tag not in ("br", "hr", "img", "input"):
                self.skip += 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            # \x01id\x02 marks where the section with that id starts (page() cuts sections there, then drops marks)
            self.out.append("\n\n" + (f"\x01{a['id']}\x02" if a.get("id") else "") + "#" * int(tag[1]) + " ")
            if a.get("id"):
                self.anchors[a["id"]] = int(tag[1])
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
        elif tag == "li":
            self.out.append("\n" + "  " * (len(self.lists) - 1) + ("1. " if self.lists[-1:] == ["ol"] else "- "))
        elif tag in ("td", "th"):
            self.out.append(" | " if self.cell else "| ")
            self.cell = True
        elif tag == "tr":
            self.out.append("\n")
            self.cell = False
        elif tag == "p" and "admonition-title" in cls:
            self.out.append("\n\n> **")
            self.adm_title = True
            return
        elif tag == "div" and "tabbed-labels" in cls:
            self.out.append("\nTabs: ")
            self.tab_labels = 1
            return
        elif tag == "label" and self.tab_labels:
            if self.tab_labels > 1:
                self.out.append(" | ")
            self.tab_labels += 1
        elif tag in ("div", "details") and "admonition" in cls or tag == "details":
            self.out.append("\n")
        elif tag == "a" and a.get("href"):
            self.href = urljoin(self.url, a["href"])
            self.link_text = []
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")
        elif tag == "code" and not self.out[-1:] == ["```\n"]:
            self.out.append("`")
        elif tag == "pre":
            self.out.append("\n```\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        if tag in self.BLOCK and tag not in ("ul", "ol", "tr", "pre"):
            self.out.append("\n\n" if tag == "p" else "\n")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "article":
            self.depth -= 1
            return
        if self.skip:
            if tag not in ("br", "hr", "img", "input"):
                self.skip -= 1
            return
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.out.append("\n")
        elif tag == "a" and self.href is not None:
            text = "".join(self.link_text).strip()
            if text and not self.href.startswith(self.url + "#"):
                self.out.append(f" ({self.href})")
            self.href = None
        elif tag == "code" and self.out[-1:] != ["```\n"]:
            self.out.append("`")
        elif tag == "pre":
            self.out.append("\n```\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "p" and self.adm_title:
            self.out.append(":**\n")
            self.adm_title = False
        elif tag == "div" and self.tab_labels:
            self.out.append("\n")
            self.tab_labels = 0
        if tag == "table":
            self.out.append("\n")

    def handle_data(self, data):
        if not self.depth or self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        self.out.append(data if self.out[-1:] == ["\n```\n"] or "```" in "".join(self.out[-2:]) else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        """The text, heading marks kept (see handle_starttag)."""
        t = "".join(self.out)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip() + "\n"


def page(ref: str) -> str:
    """A page (path or a URL on the docs site; a #anchor keeps only that section) as text, with its URL on top."""
    ref = ref.strip()
    base = _base()
    if ref.startswith("http"):
        u = urlparse(ref)
        if u.netloc != urlparse(base).netloc:
            raise HarnessError(f"{ref}: only {base} pages")
        path, anchor = _rel(u.path), u.fragment
    else:
        path, _, anchor = ref.lstrip("/").partition("#")
    if path and not path.endswith("/") and not path.endswith(".html"):
        path += "/"
    url = urljoin(base, path)
    p = _Text(url)
    p.feed(_get(path))
    text = p.text()
    if anchor:
        if anchor not in p.anchors:
            raise HarnessError(f"no section #{anchor} on {url}; its sections: {', '.join(p.anchors) or 'none'}")
        start = text.index(f"\x01{anchor}\x02")
        level = p.anchors[anchor]
        end = len(text)
        for m in re.finditer(r"\x01([^\x02]+)\x02", text[start + 1:]):
            if p.anchors.get(m.group(1), 9) <= level:        # the next heading at this level or above
                end = start + 1 + m.start()
                break
        text = text[start:end].strip() + "\n"
        url += f"#{anchor}"
    return f"{url}\n\n{re.sub(r'\x01[^\x02]*\x02', '', text)}"
