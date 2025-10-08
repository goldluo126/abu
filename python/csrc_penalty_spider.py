"""Spider for CSRC administrative penalty decisions.

This module provides a command-line program able to crawl the entire
"行政处罚决定" (Administrative Penalty Decisions) section of the CSRC web
site.  It sequentially downloads every list page (197 pages in total at
write time), follows each entry to its detail page, and stores both the
HTML fragment and text content of the decision locally.

Because the CSRC site sometimes rejects requests that look like bots, the
spider emulates a regular browser by supplying common HTTP headers and it
supports retry with exponential back-off.

The collected decisions are written to a JSON Lines file.  Each record
contains the following keys:

```
{
    "page": int,              # the list page number (1-indexed)
    "title": str,             # headline shown on the list page
    "date": "YYYY-MM-DD",    # publication date shown on the list page
    "url": str,               # absolute URL of the decision detail page
    "content_html": str,      # extracted HTML fragment of the decision
    "content_text": str       # human readable plain text version
}
```

Example
-------

```
python -m csrc_penalty_spider --output data/csrc_penalties.jsonl
```

The spider purposefully avoids any third-party dependencies to ease
execution in restricted environments.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import gzip
import zlib

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.csrc.gov.cn/csrc/c100035/zfxxgk_zdgk.shtml"
TOTAL_PAGES = 197
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Tags that should translate into explicit line breaks when converting HTML to text.
BREAK_TAGS_PATTERN = re.compile(
    r"(?is)<\s*/?(?:p|br|div|tr|li|h[1-6]|section|article|table|ul|ol)\b[^>]*>"
)
TAG_PATTERN = re.compile(r"(?s)<[^>]+>")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class ArticleSummary:
    """Metadata scraped from a list page."""

    page: int
    title: str
    date: str
    url: str


@dataclass
class ArticleContent:
    """Full detail of an article."""

    summary: ArticleSummary
    content_html: str
    content_text: str


class ArticleListParser(HTMLParser):
    """Parse the list of articles from a CSRC list page.

    The parser is purposely lenient and only looks for the minimal set of
    information needed to locate article hyperlinks and publication dates.
    """

    def __init__(self) -> None:
        super().__init__()
        self._articles: List[Tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._capture_title: bool = False
        self._title_chunks: List[str] = []
        self._pending_date: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            attr_dict = dict(attrs)
            href = attr_dict.get("href")
            if href:
                self._current_href = href.strip()
                self._capture_title = True
                self._title_chunks.clear()
        elif tag.lower() in {"span", "em", "i"}:
            attr_dict = dict(attrs)
            cls = (attr_dict.get("class") or "").lower()
            # Many CSRC list pages wrap the date inside <span class="date"> or
            # simply <span>...</span>.  We opportunistically capture any span
            # whose text matches a date pattern.
            if "date" in cls or not cls:
                self._pending_date = ""

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "a" and self._current_href and self._capture_title:
            title = unescape("".join(self._title_chunks).strip())
            if title:
                self._articles.append((self._current_href, title))
            self._current_href = None
            self._capture_title = False
            self._title_chunks.clear()
        elif tag_lower in {"span", "em", "i"} and self._pending_date is not None:
            date_text = self._pending_date.strip()
            if DATE_PATTERN.search(date_text):
                if self._articles:
                    href, title = self._articles[-1]
                    # Replace last entry with tuple containing date appended.
                    self._articles[-1] = (href, title + "\u0000" + date_text)
            self._pending_date = None

    def handle_data(self, data: str) -> None:
        if self._capture_title and self._current_href:
            self._title_chunks.append(data)
        elif self._pending_date is not None:
            self._pending_date += data

    def get_articles(self) -> List[Tuple[str, str, Optional[str]]]:
        """Return parsed articles.

        The method splits the combined title/date placeholder and ensures the
        date is stored separately from the title.  The date can be ``None`` if
        it was missing or malformed on the page.
        """

        extracted: List[Tuple[str, str, Optional[str]]] = []
        for href, title_and_date in self._articles:
            if "\u0000" in title_and_date:
                title, date = title_and_date.split("\u0000", 1)
                extracted.append((href, title.strip(), date.strip()))
            else:
                extracted.append((href, title_and_date.strip(), None))
        return extracted


class ContentExtractor(HTMLParser):
    """Extract the main article content from a CSRC detail page.

    The CSRC website uses a few different wrappers for the main article
    content (e.g. ``id="zoom"``, ``class="Custom_UnionStyle"``).  This parser
    looks for a configurable list of ids and classes and returns the HTML
    contained within the first match.  If multiple matches are found, the
    longest snippet is returned.
    """

    TARGET_IDS = {
        "zoom",
        "content",
        "article",
        "articleContent",
        "container",
        "zoomcon",
    }
    TARGET_CLASSES = {
        "Custom_UnionStyle",
        "article-content",
        "article-con",
        "content",
        "news-con",
        "TRS_Editor",
        "main-text",
        "inner-content",
    }

    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._capture_depth = 0
        self._buffer: List[str] = []
        self._snippets: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_dict = dict(attrs)
        classes = set((attr_dict.get("class") or "").replace(";", " ").split())
        element_id = attr_dict.get("id")

        should_capture = False
        if element_id and element_id in self.TARGET_IDS:
            should_capture = True
        if not should_capture and classes.intersection(self.TARGET_CLASSES):
            should_capture = True

        if should_capture and not self._capturing:
            self._capturing = True
            self._capture_depth = 0
            self._buffer.clear()

        if self._capturing:
            self._capture_depth += 1
            self._buffer.append(self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._capturing:
            self._buffer.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if self._capturing:
            self._buffer.append(f"</{tag}>")
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                snippet = "".join(self._buffer).strip()
                if snippet:
                    self._snippets.append(snippet)
                self._capturing = False
                self._buffer.clear()

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)

    def get_content(self) -> Optional[str]:
        if not self._snippets:
            return None
        # Return the longest snippet, assuming it corresponds to the main body.
        return max(self._snippets, key=len)


class CsrcPenaltySpider:
    """Crawler able to fetch administrative penalty decisions from CSRC."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        total_pages: int = TOTAL_PAGES,
        pause: Tuple[float, float] = (0.5, 1.5),
        max_retries: int = 5,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = base_url
        self.total_pages = total_pages
        self.pause = pause
        self.max_retries = max_retries
        self.timeout = timeout
        self.user_agent = user_agent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def crawl(self, start_page: int = 1, end_page: Optional[int] = None) -> List[ArticleContent]:
        """Crawl the configured page range and return the collected articles."""

        if start_page < 1:
            raise ValueError("start_page must be >= 1")
        if end_page is None:
            end_page = self.total_pages
        if end_page < start_page:
            raise ValueError("end_page must be >= start_page")
        if end_page > self.total_pages:
            raise ValueError("end_page cannot exceed configured total pages")

        results: List[ArticleContent] = []
        for page_number, page_url in enumerate(
            self._iter_page_urls(start_page, end_page), start=start_page
        ):
            LOGGER.info("Fetching list page %s: %s", page_number, page_url)
            html = self._fetch_text(page_url)
            summaries = self._parse_list_page(html, page_number, page_url)
            LOGGER.info("Page %s yielded %s articles", page_number, len(summaries))
            for summary in summaries:
                try:
                    article = self._fetch_detail(summary)
                except Exception as exc:  # pragma: no cover - defensive logging
                    LOGGER.exception("Failed to fetch %s: %s", summary.url, exc)
                    continue
                results.append(article)
                self._respectful_pause()
            self._respectful_pause()
        return results

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------
    def _fetch(self, url: str) -> bytes:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Referer": "https://www.csrc.gov.cn/",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                request = Request(url, headers=headers)
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    if "gzip" in encoding:
                        try:
                            data = gzip.decompress(data)
                        except OSError:
                            pass
                    elif "deflate" in encoding:
                        try:
                            data = zlib.decompress(data)
                        except zlib.error:
                            try:
                                data = zlib.decompress(data, -zlib.MAX_WBITS)
                            except zlib.error:
                                pass
                    return data
            except HTTPError as exc:
                if exc.code >= 500 and attempt < self.max_retries:
                    sleep_time = self._retry_delay(attempt)
                    LOGGER.warning(
                        "HTTP %s for %s (attempt %s/%s), retrying in %.2fs",
                        exc.code,
                        url,
                        attempt,
                        self.max_retries,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue
                raise
            except URLError as exc:
                if attempt < self.max_retries:
                    sleep_time = self._retry_delay(attempt)
                    LOGGER.warning(
                        "Network error for %s (attempt %s/%s): %s, retrying in %.2fs",
                        url,
                        attempt,
                        self.max_retries,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue
                raise

    def _fetch_text(self, url: str) -> str:
        raw = self._fetch(url)
        for encoding in self._candidate_encodings(raw):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        # Last resort: ignore errors.
        return raw.decode("utf-8", errors="ignore")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    def _parse_list_page(
        self, html: str, page_number: int, page_url: str
    ) -> List[ArticleSummary]:
        parser = ArticleListParser()
        parser.feed(html)
        articles = parser.get_articles()
        summaries: List[ArticleSummary] = []
        for href, title, date in articles:
            absolute_url = urljoin(page_url, href)
            date_str = date if date and DATE_PATTERN.fullmatch(date) else ""
            summaries.append(
                ArticleSummary(
                    page=page_number,
                    title=title.strip(),
                    date=date_str,
                    url=absolute_url,
                )
            )
        return summaries

    def _fetch_detail(self, summary: ArticleSummary) -> ArticleContent:
        LOGGER.info("Fetching detail: %s", summary.url)
        html = self._fetch_text(summary.url)
        content_html = self._extract_content_html(html)
        content_text = html_to_text(content_html) if content_html else html_to_text(html)
        return ArticleContent(summary=summary, content_html=content_html or "", content_text=content_text)

    def _extract_content_html(self, html: str) -> Optional[str]:
        extractor = ContentExtractor()
        extractor.feed(html)
        content = extractor.get_content()
        if content:
            return content
        # Fallback: attempt to locate <body> content.
        match = re.search(r"(?is)<body[^>]*>(.*?)</body>", html)
        if match:
            return match.group(1)
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _iter_page_urls(self, start_page: int, end_page: int) -> Iterator[str]:
        base, suffix = self.base_url.rsplit(".", 1)
        for index in range(start_page, end_page + 1):
            if index == 1 and start_page == 1:
                yield self.base_url
            else:
                yield f"{base}_{index - 1}.{suffix}"

    def _retry_delay(self, attempt: int) -> float:
        base_delay = min(8.0, 2 ** (attempt - 1))
        jitter = random.uniform(0.0, 0.5)
        return base_delay + jitter

    def _respectful_pause(self) -> None:
        low, high = self.pause
        if high <= 0:
            return
        delay = random.uniform(low, high)
        time.sleep(delay)

    @staticmethod
    def _candidate_encodings(raw: bytes) -> Sequence[str]:
        # Order matters: try explicit declaration first, then common fallbacks.
        candidates: List[str] = []
        if raw:
            # Quick sniff for UTF-8 BOM.
            if raw.startswith(b"\xef\xbb\xbf"):
                candidates.append("utf-8-sig")
        # Inspect meta tag if possible.
        snippet = raw[:1024].decode("ascii", errors="ignore")
        match = re.search(r"charset=([\w-]+)", snippet, re.IGNORECASE)
        if match:
            candidates.append(match.group(1).lower())
        candidates.extend(["utf-8", "gb18030", "gbk", "gb2312"])
        # Remove duplicates while preserving order.
        seen = set()
        ordered: List[str] = []
        for enc in candidates:
            if enc not in seen:
                ordered.append(enc)
                seen.add(enc)
        return ordered


def html_to_text(html_fragment: str) -> str:
    """Convert an HTML fragment into human-readable text."""

    if not html_fragment:
        return ""
    fragment = html_fragment
    fragment = BREAK_TAGS_PATTERN.sub("\n", fragment)
    fragment = TAG_PATTERN.sub("", fragment)
    text = unescape(fragment)
    text = text.replace("\r", "")
    text = re.sub(r"\u3000", " ", text)
    text = re.sub(r"\xa0", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def save_as_jsonl(records: Iterable[ArticleContent], output_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        for record in records:
            data = {
                "page": record.summary.page,
                "title": record.summary.title,
                "date": record.summary.date,
                "url": record.summary.url,
                "content_html": record.content_html,
                "content_text": record.content_text,
            }
            json.dump(data, fh, ensure_ascii=False)
            fh.write("\n")


def parse_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl CSRC administrative penalty decisions and store them locally.",
    )
    parser.add_argument(
        "--output",
        default="data/csrc_penalties.jsonl",
        help="Path of the JSON Lines file to create (default: %(default)s)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First list page to crawl (1-indexed).",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        default=TOTAL_PAGES,
        help="Last list page to crawl (inclusive).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: %(default)s)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        nargs=2,
        metavar=("MIN", "MAX"),
        default=(0.5, 1.5),
        help="Random pause range (in seconds) between requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Network timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Number of retries for failed HTTP requests (default: %(default)s)",
    )
    return parser.parse_args(args)


def main(argv: Optional[Sequence[str]] = None) -> None:
    options = parse_args(argv)
    logging.basicConfig(level=getattr(logging, options.log_level))
    spider = CsrcPenaltySpider(
        pause=tuple(options.pause),
        timeout=options.timeout,
        max_retries=options.max_retries,
    )
    records = spider.crawl(start_page=options.start_page, end_page=options.end_page)
    save_as_jsonl(records, options.output)
    LOGGER.info("Saved %s decisions to %s", len(records), options.output)


if __name__ == "__main__":
    main()
