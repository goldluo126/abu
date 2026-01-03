#!/usr/bin/env python3
"""Crawler for CSRC administrative penalty decisions.

Usage:
  python csrc_penalty_crawler.py --start 1 --end 200 --out data

Outputs:
  - data/items.jsonl: line-delimited JSON records
  - data/items/XXXX_*.md: per-item Markdown files
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from typing import Iterable, List, Optional
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://www.csrc.gov.cn/csrc/c101971/zfxxgk_zdgk.shtml"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
DATE_RE = re.compile(r"\d{4}[-年]\d{1,2}[-月]\d{1,2}日?")


@dataclass
class ListItem:
    title: str
    url: str
    date: Optional[str]
    page: int
    seq: Optional[int] = None


@dataclass
class DetailItem:
    title: str
    url: str
    date: Optional[str]
    content: str
    list_page: int
    list_title: str
    list_date: Optional[str]
    seq: Optional[int]


class ListPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: List[ListItem] = []
        self._current_row: List[str] = []
        self._current_href: Optional[str] = None
        self._current_title: Optional[str] = None
        self._in_a = False
        self._in_tr = False
        self._in_td = False
        self._in_li = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "tr":
            self._in_tr = True
            self._current_row = []
            self._current_href = None
            self._current_title = None
        elif tag == "li":
            self._in_li = True
            self._current_row = []
            self._current_href = None
            self._current_title = None
        elif tag == "td" and self._in_tr:
            self._in_td = True
        elif tag == "a":
            href = attr.get("href")
            if href:
                self._in_a = True
                self._current_href = href

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False
        elif tag == "tr":
            self._commit_current_row()
            self._in_tr = False
            self._in_td = False
        elif tag == "li":
            self._commit_current_row()
            self._in_li = False
        elif tag == "td":
            self._in_td = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_a and self._current_href:
            title = re.sub(r"\s+", " ", text)
            if not self._current_title:
                self._current_title = title
        if self._in_tr or self._in_li:
            self._current_row.append(text)

    def _commit_current_row(self):
        if not self._current_href or not self._current_title:
            return
        date = None
        for cell in self._current_row:
            match = DATE_RE.search(cell)
            if match:
                date = match.group(0)
                break
        self.items.append(
            ListItem(title=self._current_title, url=self._current_href, date=date, page=0)
        )
        self._current_row = []
        self._current_href = None
        self._current_title = None


class DetailPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture = False
        self._capture_depth = 0
        self._content_parts: List[str] = []
        self._all_parts: List[str] = []
        self.title: Optional[str] = None
        self.date: Optional[str] = None
        self._in_heading = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls = attr.get("class", "")
        elem_id = attr.get("id", "")
        if tag in {"h1", "h2"}:
            self._in_heading = True
        if (
            "TRS_Editor" in cls
            or "article-content" in cls
            or "xxgk_content" in cls
            or "gk_content" in cls
            or "content" == elem_id
            or "content" in cls
            or "article" in cls
            or "view" in cls
            or "detail" in cls
        ):
            self._capture = True
            self._capture_depth += 1

    def handle_endtag(self, tag):
        if self._capture and tag in {"div", "article", "section"}:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                self._capture = False
                self._capture_depth = 0
        if tag in {"h1", "h2"}:
            self._in_heading = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_heading:
            self.title = text
        elif not self.title and len(text) >= 4:
            if "处罚" in text or "决定" in text:
                self.title = text
        if not self.date and DATE_RE.search(text):
            self.date = DATE_RE.search(text).group(0)
        self._all_parts.append(text)
        if self._capture:
            self._content_parts.append(text)

    def content(self) -> str:
        parts = self._content_parts or self._all_parts
        content = "\n".join(parts)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()


def cache_filename(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + ".html"


def read_cache(cache_dir: str, url: str) -> Optional[str]:
    path = os.path.join(cache_dir, cache_filename(url))
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_cache(cache_dir: str, url: str, content: str) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, cache_filename(url))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def fetch(
    url: str,
    timeout: int = 30,
    cache_dir: Optional[str] = None,
    offline: bool = False,
) -> str:
    if offline and cache_dir:
        cached = read_cache(cache_dir, url)
        if cached is None:
            raise FileNotFoundError(f"Cache miss for {url}")
        return cached

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            content = resp.read().decode(charset, "ignore")
    except Exception as exc:
        if cache_dir:
            cached = read_cache(cache_dir, url)
            if cached is not None:
                print(
                    f"Warning: fetch failed for {url}, using cache. ({exc})",
                    file=sys.stderr,
                )
                return cached
        raise

    if cache_dir:
        write_cache(cache_dir, url, content)
    return content


def page_url(page: int) -> str:
    if page <= 1:
        return BASE_URL
    return BASE_URL.replace(".shtml", f"_{page - 1}.shtml")


def parse_list_page(html: str, page: int) -> List[ListItem]:
    parser = ListPageParser()
    parser.feed(html)
    items: List[ListItem] = []
    for item in parser.items:
        item.page = page
        items.append(item)
    return items


def parse_detail_page(html: str, url: str, list_item: ListItem) -> DetailItem:
    parser = DetailPageParser()
    parser.feed(html)
    title = parser.title or list_item.title
    content = parser.content()
    date = parser.date or list_item.date
    return DetailItem(
        title=title,
        url=url,
        date=date,
        content=content,
        list_page=list_item.page,
        list_title=list_item.title,
        list_date=list_item.date,
        seq=list_item.seq,
    )


def sanitize_filename(text: str) -> str:
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[\\/:*?\"<>|]", "", text)
    return text[:80]


def write_outputs(items: Iterable[DetailItem], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    items_dir = os.path.join(out_dir, "items")
    os.makedirs(items_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, "items.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for idx, item in enumerate(items, start=1):
            record = asdict(item)
            record["index"] = idx
            jf.write(json.dumps(record, ensure_ascii=False) + "\n")
            filename = f"{idx:04d}_{sanitize_filename(item.title)}.md"
            md_path = os.path.join(items_dir, filename)
            with open(md_path, "w", encoding="utf-8") as mf:
                mf.write(f"# {item.title}\n\n")
                if item.date:
                    mf.write(f"**发布日期**: {item.date}\n\n")
                mf.write(f"**来源页**: {item.url}\n\n")
                mf.write(item.content or "")


def crawl(
    start: int,
    end: int,
    sleep: float = 0.6,
    cache_dir: Optional[str] = None,
    offline: bool = False,
) -> List[DetailItem]:
    all_items: List[DetailItem] = []
    seq = 0
    for page in range(start, end + 1):
        list_html = fetch(page_url(page), cache_dir=cache_dir, offline=offline)
        list_items = parse_list_page(list_html, page)
        for item in list_items:
            seq += 1
            item.seq = seq
            detail_url = urljoin(BASE_URL, item.url)
            detail_html = fetch(detail_url, cache_dir=cache_dir, offline=offline)
            detail = parse_detail_page(detail_html, detail_url, item)
            all_items.append(detail)
            time.sleep(sleep)
        time.sleep(sleep)
    return all_items


def main() -> None:
    parser = argparse.ArgumentParser(description="CSRC administrative penalty crawler")
    parser.add_argument("--start", type=int, default=1, help="start page")
    parser.add_argument("--end", type=int, default=200, help="end page")
    parser.add_argument("--out", type=str, default="csrc_data", help="output directory")
    parser.add_argument("--sleep", type=float, default=0.6, help="sleep seconds between requests")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="cache directory for downloaded HTML (enables offline reuse)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="only read from cache; do not fetch from network",
    )
    args = parser.parse_args()

    items = crawl(
        args.start,
        args.end,
        args.sleep,
        cache_dir=args.cache_dir,
        offline=args.offline,
    )
    write_outputs(items, args.out)
    print(f"Saved {len(items)} items to {args.out}")


if __name__ == "__main__":
    main()
