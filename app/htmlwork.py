from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from .translator import should_translate_text

DATA_DIR = Path("/data")
ORIGINAL_DIR = DATA_DIR / "original"
TRANSLATED_DIR = DATA_DIR / "translated"

SKIP_TAGS = {"script", "style", "pre", "code", "math", "svg", "noscript", "textarea"}
SKIP_CLASS_PARTS = {
    "formula", "math", "katex", "tex", "highlight", "code", "source", "username",
    "user-info", "user_info", "tm-user-info", "tm-user-info__username", "author",
}
ARTICLE_SELECTORS = [
    "article.tm-article-presenter__content",
    ".tm-article-body",
    "#post-content-body",
    "article",
]
COMMENT_SELECTORS = [
    ".tm-comment__body-content",
    ".comment__message",
    ".tm-comment-thread__comment",
]


def validate_habr_url(url: str) -> str:
    url = url.strip()

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("http 또는 https URL만 사용할 수 있습니다.")

    if host not in {"habr.com", "www.habr.com"}:
        raise ValueError("habr.com URL만 입력할 수 있습니다.")

    return url


def slug_for(url: str) -> str:
    article = re.search(r"/articles/(\d+)", url)
    base = article.group(1) if article else hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"habr-{base}"


def download_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "ru,en;q=0.8",
    }
    with httpx.Client(follow_redirects=True, timeout=45, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        final_host = (urlparse(str(response.url)).hostname or "").lower()
        if final_host not in {"habr.com", "www.habr.com"}:
            raise ValueError("Habr 외부 주소로 리디렉션되었습니다.")
        return response.text


def _has_skip_ancestor(node: NavigableString) -> bool:
    parent = node.parent
    while isinstance(parent, Tag):
        if parent.name and parent.name.lower() in SKIP_TAGS:
            return True
        classes = " ".join(parent.get("class", [])) if parent.has_attr("class") else ""
        ident = parent.get("id", "")
        marker = f"{classes} {ident}".lower()
        if any(part in marker for part in SKIP_CLASS_PARTS):
            return True
        if parent.name == "a" and parent.get("href"):
            href = str(parent.get("href"))
            # Keep usernames and URL labels intact. Ordinary prose links remain translatable.
            if "/users/" in href or should_translate_text(str(node)) is False:
                return True
        parent = parent.parent
    return False


def _root_nodes(soup: BeautifulSoup) -> list[Tag]:
    roots: list[Tag] = []
    for selector in ARTICLE_SELECTORS:
        found = soup.select_one(selector)
        if found:
            roots.append(found)
            break
    for selector in COMMENT_SELECTORS:
        roots.extend(soup.select(selector))
    # Deduplicate while preserving DOM order.
    seen = set()
    unique = []
    for root in roots:
        ident = id(root)
        if ident not in seen:
            seen.add(ident)
            unique.append(root)
    return unique


def extract_nodes(html: str) -> tuple[BeautifulSoup, list[NavigableString]]:
    soup = BeautifulSoup(html, "lxml")
    nodes: list[NavigableString] = []
    for root in _root_nodes(soup):
        for text_node in root.find_all(string=True):
            if _has_skip_ancestor(text_node):
                continue
            if should_translate_text(str(text_node)):
                nodes.append(text_node)
    return soup, nodes


def xpath_for(node: NavigableString) -> str:
    parts = []
    current = node.parent
    while isinstance(current, Tag) and current.name:
        siblings = [s for s in current.parent.find_all(current.name, recursive=False)] if current.parent else [current]
        idx = siblings.index(current) + 1 if current in siblings else 1
        parts.append(f"{current.name}[{idx}]")
        current = current.parent
    return "/" + "/".join(reversed(parts))


def save_original(slug: str, html: str) -> Path:
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    path = ORIGINAL_DIR / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def sanitize_for_static_view(soup: BeautifulSoup) -> BeautifulSoup:
    """Turn the captured Habr page into a passive, read-only HTML document."""
    # Remove executable/embedded content and elements that can replace or reload the page.
    for tag in list(soup.select(
        "script, noscript, iframe, frame, frameset, object, embed, base, "
        "link[rel='modulepreload'], link[rel='preload'][as='script'], "
        "meta[http-equiv='refresh']"
    )):
        tag.decompose()

    # Remove inline event handlers and javascript: URLs.
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        for attr in ("href", "src", "action", "formaction"):
            value = tag.get(attr)
            if isinstance(value, str) and value.lstrip().lower().startswith("javascript:"):
                del tag.attrs[attr]

    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html is not None:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)

    # Defense in depth: even if an executable element slips through, the browser blocks it.
    for old in list(soup.head.select("meta[http-equiv='Content-Security-Policy']")):
        old.decompose()
    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = (
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline' https:; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; "
        "media-src 'self' data: https:; "
        "script-src 'none'; connect-src 'none'; "
        "frame-src 'none'; object-src 'none'; worker-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    soup.head.insert(0, csp)
    return soup


def save_translated(slug: str, soup: BeautifulSoup) -> Path:
    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSLATED_DIR / f"{slug}.html"
    sanitize_for_static_view(soup)
    path.write_text(str(soup), encoding="utf-8")
    return path
