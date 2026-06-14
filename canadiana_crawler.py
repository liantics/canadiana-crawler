#!/usr/bin/env python3
"""Crawl a Canadiana/Héritage directory page and download all page images."""

from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = "canadiana-crawler/1.0"


@dataclass(frozen=True)
class PageRef:
    index: int
    info_url: str

    @property
    def image_url(self) -> str:
        suffix = "/info.json"
        if not self.info_url.endswith(suffix):
            raise ValueError(f"Unexpected IIIF info URL: {self.info_url}")
        return f"{self.info_url[:-len(suffix)]}/full/full/0/default.jpg"


class PageSelectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_page_select = False
        self.page_refs: List[PageRef] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = dict(attrs)
        if tag == "select" and attr_map.get("id") == "pvPageSelect":
            self.in_page_select = True
            return

        if tag == "option" and self.in_page_select:
            value = attr_map.get("value")
            info_url = attr_map.get("data-uri")
            if value and info_url and value.isdigit():
                self.page_refs.append(PageRef(index=int(value), info_url=info_url))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self.in_page_select:
            self.in_page_select = False


def request_bytes(url: str, timeout: int, retries: int) -> bytes:
    delay_seconds = 1.0
    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"Request failed after {retries} attempts: {url}") from exc
            print(
                f"Retrying ({attempt}/{retries - 1}) after error for {url}: {exc}",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)
            delay_seconds *= 2
    raise RuntimeError(f"Exhausted retries for {url}")


def request_text(url: str, timeout: int, retries: int) -> str:
    raw = request_bytes(url=url, timeout=timeout, retries=retries)
    return raw.decode("utf-8", errors="replace")


def discover_pages(directory_url: str, timeout: int, retries: int) -> List[PageRef]:
    html = request_text(directory_url, timeout=timeout, retries=retries)
    parser = PageSelectParser()
    parser.feed(html)

    if not parser.page_refs:
        raise RuntimeError(
            "Could not find any pages in the directory HTML. "
            "The page structure may have changed."
        )

    return sorted(parser.page_refs, key=lambda ref: ref.index)


def write_file(path: pathlib.Path, data: bytes) -> None:
    temp_path = path.with_suffix(path.suffix + ".part")
    temp_path.write_bytes(data)
    temp_path.replace(path)


def download_page(
    page: PageRef,
    output_dir: pathlib.Path,
    pad_width: int,
    timeout: int,
    retries: int,
    overwrite: bool,
) -> tuple[int, str, str]:
    filename = f"page_{page.index:0{pad_width}d}.jpg"
    target_path = output_dir / filename

    if target_path.exists() and not overwrite:
        return page.index, "skipped", filename

    payload = request_bytes(page.image_url, timeout=timeout, retries=retries)
    write_file(target_path, payload)
    return page.index, "downloaded", filename


def filter_pages(pages: Iterable[PageRef], start: int, end: Optional[int]) -> List[PageRef]:
    return [p for p in pages if p.index >= start and (end is None or p.index <= end)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download all images from a Canadiana/Héritage directory page."
    )
    parser.add_argument(
        "directory_url",
        help="Directory URL, e.g. https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Destination folder for images (default: %(default)s)",
    )
    parser.add_argument("--start", type=int, default=1, help="First page number (default: %(default)s)")
    parser.add_argument("--end", type=int, default=None, help="Last page number (inclusive)")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel download workers (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retries per request (default: %(default)s)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files even if they already exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show discovered pages; do not download images",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.start < 1:
        print("--start must be >= 1", file=sys.stderr)
        return 2
    if args.end is not None and args.end < args.start:
        print("--end must be >= --start", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 2

    pages = discover_pages(args.directory_url, timeout=args.timeout, retries=args.retries)
    selected = filter_pages(pages, start=args.start, end=args.end)
    if not selected:
        print("No pages matched the requested range.", file=sys.stderr)
        return 1

    total_pages = len(pages)
    print(f"Discovered {total_pages} pages in directory.")
    print(f"Selected {len(selected)} page(s): {selected[0].index}..{selected[-1].index}")

    if args.dry_run:
        print("Dry run enabled. First 5 page URLs:")
        for page in selected[:5]:
            print(f"  {page.index}: {page.image_url}")
        return 0

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pad_width = len(str(total_pages))

    downloaded = 0
    skipped = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                download_page,
                page,
                output_dir,
                pad_width,
                args.timeout,
                args.retries,
                args.overwrite,
            ): page
            for page in selected
        }

        for count, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            page = futures[future]
            try:
                _, status, filename = future.result()
                if status == "downloaded":
                    downloaded += 1
                else:
                    skipped += 1
                print(f"[{count}/{len(selected)}] {status}: {filename}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[{count}/{len(selected)}] failed: page {page.index} ({exc})", file=sys.stderr)

    print(
        f"Done. downloaded={downloaded}, skipped={skipped}, failed={failed}, "
        f"output_dir={output_dir.resolve()}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
