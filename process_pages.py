#!/usr/bin/env python3
"""Process pages downloaded by canadiana_crawler.py.

Pipeline subcommands:

    organize     Split downloaded images into originals and a working copy
    pdf          Combine page images into a single PDF
    verify-pdf   Check PDF structure and page count
    ocr          Preprocess page images and OCR them to per-page text files
    extract      Parse OCR text into baptism records (CSV/TXT/JSON)

The `ocr` subcommand uses local Tesseract. It reads the typed archive cards
well but performs poorly on 18th/19th-century handwriting; the committed
extraction tables were produced from a higher-quality hosted OCR pass. Point
`extract --ocr-dir` at any directory of per-page `page_NNNN.txt` files to run
the parser over better text.

Note: Tesseract cannot read images under `/tmp` on some macOS setups, so keep
`--out-dir` outside `/tmp`.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


PAGE_GLOB = "page_*.jpg"
PAGE_NUMBER_RE = re.compile(r"page_(\d+)")

# Preprocessing defaults tuned for this reel: trim the microfilm leader and the
# archive title card, then upscale and boost contrast before OCR.
DEFAULT_CROP = (760, 1080, 650, 1280)  # left, top, right_inset, bottom_inset
DEFAULT_SCALE = 2
DEFAULT_CONTRAST = 1.8
DEFAULT_PSM = 4

# Record parsing
BAPTISM_RE = re.compile(r"(?i)\b(b[a-z]{0,3}p[a-z]{0,3}t[a-z]*|chri[a-z]{2,})\b")
CHILD_RE = re.compile(r"(?i)\b(daughter|daught|son|infant|child|children)\b")
NON_BAPTISM_RE = re.compile(r"(?i)\b(buried|married|marriage|wedlock)\b")
YEAR_RE = re.compile(r"(?<!\d)(17\d{2}|18\d{2})(?!\d)")
NAME_RE = re.compile(r"[A-Z][A-Za-z'\-]{2,}")

STOP_WORDS = {
    "parish", "george", "georges", "public", "archives", "archive", "canada",
    "banns", "church", "england", "holy", "rite", "rites", "ceremonies", "ceremony",
    "baptized", "baptised", "baptism", "bapti", "daughter", "son", "wife",
    "his", "her", "mr", "mrs", "miss", "this", "day", "book", "page",
    "buried", "married", "marriage", "wedlock", "parents", "parent",
    "godfather", "godmother", "from", "with", "and", "the", "of", "to", "in",
    "on", "for", "by", "it", "is", "at", "as", "register", "anglican",
    "sydney", "cape", "breton", "then",
}

MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}

# Period-common given names used to map noisy OCR tokens onto plausible names.
COMMON_NAMES = [
    "John", "William", "Mary", "Ann", "Anna", "James", "Thomas", "Sarah",
    "Elizabeth", "Margaret", "Catherine", "Catharine", "Peter", "Charles",
    "David", "Daniel", "Alexander", "George", "Joseph", "Samuel", "Jane",
    "Hugh", "Andrew", "Robert", "Isaac", "Martha", "Hannah", "Agnes",
    "Francis", "Michael", "Nicholas", "Donald", "Angus", "Jean", "Janet",
    "Joanna", "Joan", "Isabel", "Isabella", "Eliza", "Edward", "Henry",
    "Richard", "Philip", "Patrick", "Walter", "Harriet", "Louisa",
    "Charlotte", "Frances", "Eleanor", "Rebecca", "Rachel", "Benjamin",
    "Arthur", "Simon", "Mathew", "Matthew", "Mark", "Luke", "Paul",
    "Stephen", "Susan", "Sophia", "Marion", "Neil", "Malcolm", "Duncan",
    "Allan", "Alan", "Ellen", "Esther", "Dorothy", "Moses", "Barnabas",
    "Basil", "Mabel", "Maria",
]

NAME_MATCH_THRESHOLD = 0.72


@dataclass
class Record:
    page: int
    year: str
    names: List[str]
    confidence: str
    raw_names: List[str] = field(default_factory=list)
    snippet: str = ""

    def as_row(self) -> List[str]:
        return [self.year, str(self.page), "; ".join(self.names), self.confidence]


def page_number(path: pathlib.Path) -> int:
    match = PAGE_NUMBER_RE.search(path.stem)
    return int(match.group(1)) if match else 0


def sorted_pages(directory: pathlib.Path, pattern: str = PAGE_GLOB) -> List[pathlib.Path]:
    return sorted(directory.glob(pattern), key=page_number)


def require_dir(path: pathlib.Path, label: str) -> None:
    if not path.is_dir():
        raise SystemExit(f"{label} not found: {path}")


# --------------------------------------------------------------------------
# organize
# --------------------------------------------------------------------------

def cmd_organize(args: argparse.Namespace) -> int:
    pages_dir = pathlib.Path(args.pages_dir)
    require_dir(pages_dir, "Pages directory")

    originals = pages_dir / args.originals_name
    conversions = pages_dir / args.conversions_name
    originals.mkdir(parents=True, exist_ok=True)

    loose = sorted_pages(pages_dir)
    for image in loose:
        shutil.move(str(image), str(originals / image.name))
    print(f"Moved {len(loose)} image(s) into {originals}")

    stored = sorted_pages(originals)
    if not stored:
        print(f"No images found in {originals}", file=sys.stderr)
        return 1

    if args.no_copy:
        print(f"Originals ready: {len(stored)} image(s) in {originals}")
        return 0

    conversions.mkdir(parents=True, exist_ok=True)
    copied = 0
    for image in stored:
        target = conversions / image.name
        if target.exists() and not args.overwrite:
            continue
        shutil.copy2(image, target)
        copied += 1

    print(f"Copied {copied} image(s) into {conversions}")
    print(f"Originals: {len(stored)} | Working copies: {len(sorted_pages(conversions))}")
    return 0


# --------------------------------------------------------------------------
# pdf / verify-pdf
# --------------------------------------------------------------------------

def cmd_pdf(args: argparse.Namespace) -> int:
    try:
        import img2pdf
    except ImportError:
        raise SystemExit("img2pdf is required: pip install img2pdf")

    source_dir = pathlib.Path(args.source_dir)
    require_dir(source_dir, "Source directory")

    pages = sorted_pages(source_dir)
    if not pages:
        print(f"No images matching {PAGE_GLOB} in {source_dir}", file=sys.stderr)
        return 1

    output = pathlib.Path(args.output) if args.output else source_dir / "combined.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Combining {len(pages)} image(s) into {output}")
    with open(output, "wb") as handle:
        handle.write(img2pdf.convert([str(p) for p in pages]))

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"Wrote {output} ({size_mb:.1f} MB) from {len(pages)} source image(s)")
    return 0


def cmd_verify_pdf(args: argparse.Namespace) -> int:
    try:
        import pikepdf
    except ImportError:
        raise SystemExit("pikepdf is required: pip install pikepdf")

    pdf_path = pathlib.Path(args.pdf)
    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")

    header = pdf_path.open("rb").read(8)
    print(f"File: {pdf_path}")
    print(f"Size: {pdf_path.stat().st_size / (1024 * 1024):.1f} MB")
    print(f"Header: {header.decode('latin-1', 'ignore').strip()}")

    try:
        with pikepdf.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to parse PDF: {exc}", file=sys.stderr)
        return 1

    print(f"Pages: {page_count}")

    expected: Optional[int] = args.expect_pages
    if expected is None and args.source_dir:
        source_dir = pathlib.Path(args.source_dir)
        require_dir(source_dir, "Source directory")
        expected = len(sorted_pages(source_dir))
        print(f"Source images: {expected}")

    if expected is not None and page_count != expected:
        print(f"Page count mismatch: pdf={page_count} expected={expected}", file=sys.stderr)
        return 1

    print("PDF verified.")
    return 0


# --------------------------------------------------------------------------
# ocr
# --------------------------------------------------------------------------

def preprocess_image(source: pathlib.Path, target: pathlib.Path, args: argparse.Namespace):
    from PIL import Image, ImageEnhance, ImageOps

    left, top, right_inset, bottom_inset = args.crop
    image = Image.open(source).convert("L")
    width, height = image.size

    right = width - right_inset
    bottom = height - bottom_inset
    if right <= left or bottom <= top:
        raise ValueError(f"Crop box collapses for {source.name} ({width}x{height})")

    image = image.crop((left, top, right, bottom))
    if args.scale != 1:
        image = image.resize(
            (image.width * args.scale, image.height * args.scale),
            Image.Resampling.LANCZOS,
        )
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(args.contrast)
    image.save(target)
    return image.size


def run_tesseract(image_path: pathlib.Path, psm: int) -> str:
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "--psm", str(psm)],
        capture_output=True,
    )
    # Tesseract writes diagnostics to stderr and can emit non-UTF-8 bytes;
    # decode leniently and treat partial output as usable.
    text = (result.stdout or b"").decode("utf-8", "ignore")

    if result.returncode != 0 and not text.strip():
        detail = (result.stderr or b"").decode("utf-8", "ignore").strip()
        last_line = detail.splitlines()[-1] if detail else "no diagnostics"
        raise RuntimeError(f"tesseract exited {result.returncode}: {last_line}")

    return text


def cmd_ocr(args: argparse.Namespace) -> int:
    if shutil.which("tesseract") is None:
        raise SystemExit("tesseract not found on PATH (macOS: brew install tesseract)")
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit("Pillow is required: pip install Pillow")

    source_dir = pathlib.Path(args.source_dir)
    require_dir(source_dir, "Source directory")

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = out_dir / "preprocessed"
    if args.keep_images:
        image_dir.mkdir(parents=True, exist_ok=True)

    pages = [
        p for p in sorted_pages(source_dir)
        if page_number(p) >= args.start and (args.end is None or page_number(p) <= args.end)
    ]
    if not pages:
        print("No pages matched the requested range.", file=sys.stderr)
        return 1

    print(f"OCR over {len(pages)} page(s) from {source_dir}")
    processed = 0
    skipped = 0
    empty = 0
    failed = 0

    for index, source in enumerate(pages, start=1):
        number = page_number(source)
        text_path = out_dir / f"page_{number:04d}.txt"
        if text_path.exists() and not args.overwrite:
            skipped += 1
            continue

        target_dir = image_dir if args.keep_images else out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        processed_image = target_dir / f"page_{number:04d}.png"

        try:
            preprocess_image(source, processed_image, args)
            text = run_tesseract(processed_image, args.psm)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[{index}/{len(pages)}] failed: page {number} ({exc})", file=sys.stderr)
            continue
        finally:
            if not args.keep_images and processed_image.exists():
                processed_image.unlink()

        text_path.write_text(text, encoding="utf-8", errors="ignore")
        processed += 1
        if not text.strip():
            empty += 1
        print(f"[{index}/{len(pages)}] page {number}: {len(text.strip())} chars")

    print(
        f"Done. processed={processed}, skipped={skipped}, empty={empty}, "
        f"failed={failed}, out_dir={out_dir.resolve()}"
    )
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def normalize_names(snippet: str) -> List[str]:
    """Map noisy OCR tokens onto plausible period-common given names."""
    names: List[str] = []
    for token in re.findall(r"[A-Za-z]{3,}", snippet):
        lowered = token.lower()
        if lowered in STOP_WORDS or lowered in MONTH_WORDS:
            continue
        if BAPTISM_RE.fullmatch(token):
            continue

        best: Optional[str] = None
        best_score = 0.0
        for candidate in COMMON_NAMES:
            score = difflib.SequenceMatcher(None, lowered, candidate.lower()).ratio()
            if score > best_score:
                best_score = score
                best = candidate

        if best and best_score >= NAME_MATCH_THRESHOLD and best not in names:
            names.append(best)
    return names


def raw_name_tokens(snippet: str) -> List[str]:
    tokens: List[str] = []
    for token in NAME_RE.findall(snippet):
        if token.lower() in STOP_WORDS or token.lower() in MONTH_WORDS:
            continue
        if not re.search(r"[aeiouyAEIOUY]", token):
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:8]


def score_confidence(has_baptism: bool, year: str, token_count: int) -> str:
    """Grade a row.

    Scored on the count of raw OCR name tokens rather than normalized names:
    normalization discards anything below the fuzzy-match threshold, so using
    it here would silently downgrade rows that are well supported in the
    source text.
    """
    if has_baptism and year != "Unknown" and token_count >= 4:
        return "high"
    if has_baptism and year != "Unknown" and token_count >= 2:
        return "medium"
    return "low"


def parse_page(page: int, text: str) -> List[Record]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    # Skip pages with no baptism marker anywhere; a bare "son"/"daughter" on a
    # marriage or burial page is not evidence of a baptism entry.
    if not any(BAPTISM_RE.search(line) for line in lines):
        return []

    page_years = [year for line in lines for year in YEAR_RE.findall(line)]
    records: List[Record] = []
    seen: set = set()

    for index, line in enumerate(lines):
        if "register of baptisms" in line.lower():
            continue

        has_baptism = bool(BAPTISM_RE.search(line))
        near_baptism = False
        if CHILD_RE.search(line) and not has_baptism:
            window = lines[max(0, index - 3): index + 4]
            near_baptism = any(BAPTISM_RE.search(other) for other in window)

        if not (has_baptism or near_baptism):
            continue
        if NON_BAPTISM_RE.search(line) and not has_baptism:
            continue

        context_lines = lines[max(0, index - 2): index + 4]
        snippet = " | ".join(context_lines)

        years: List[str] = []
        for other in lines[max(0, index - 5): index + 6]:
            years.extend(YEAR_RE.findall(other))
        if not years:
            years = page_years[:1]
        year = years[0] if years else "Unknown"

        context = " ".join(context_lines)
        tokens = raw_name_tokens(context)
        if not tokens:
            continue

        names = normalize_names(context)
        if not names:
            continue

        key = (year, tuple(names[:5]), snippet[:100])
        if key in seen:
            continue
        seen.add(key)

        records.append(
            Record(
                page=page,
                year=year,
                names=names,
                confidence=score_confidence(has_baptism, year, len(tokens)),
                raw_names=tokens,
                snippet=snippet,
            )
        )

    return records


def drop_subset_duplicates(records: Sequence[Record]) -> List[Record]:
    """Collapse rows whose names are a subset of a richer row on the same page/year."""
    groups: Dict[tuple, List[Record]] = {}
    for record in records:
        groups.setdefault((record.page, record.year), []).append(record)

    kept: List[Record] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda r: (-len(r.names), r.confidence != "high"))
        chosen: List[Record] = []
        for record in ordered:
            names = set(record.names)
            if any(names.issubset(set(other.names)) for other in chosen):
                continue
            chosen.append(record)
        kept.extend(chosen)

    return kept


def cmd_extract(args: argparse.Namespace) -> int:
    ocr_dir = pathlib.Path(args.ocr_dir)
    require_dir(ocr_dir, "OCR directory")

    text_files = sorted(ocr_dir.glob("page_*.txt"), key=page_number)
    if not text_files:
        print(f"No page_*.txt files in {ocr_dir}", file=sys.stderr)
        return 1

    records: List[Record] = []
    for path in text_files:
        number = page_number(path)
        if number < args.start or (args.end is not None and number > args.end):
            continue
        records.extend(parse_page(number, path.read_text(encoding="utf-8", errors="ignore")))

    filtered: List[Record] = []
    for record in records:
        if record.year == "Unknown":
            if args.require_year:
                continue
        else:
            year = int(record.year)
            if year < args.min_year or year > args.max_year:
                continue
        filtered.append(record)

    kept = drop_subset_duplicates(filtered)
    kept.sort(key=lambda r: (9999 if r.year == "Unknown" else int(r.year), r.page))

    prefix = pathlib.Path(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)

    csv_path = prefix.with_name(prefix.name + ".csv")
    txt_path = prefix.with_name(prefix.name + ".txt")
    json_path = prefix.with_name(prefix.name + ".json")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["year", "page", "names", "confidence"])
        for record in kept:
            writer.writerow(record.as_row())

    with txt_path.open("w", encoding="utf-8") as handle:
        handle.write("year\tpage\tnames\tconfidence\n")
        for record in kept:
            handle.write("\t".join(record.as_row()) + "\n")

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "page": r.page,
                    "year": r.year,
                    "names": r.names,
                    "confidence": r.confidence,
                    "raw_names": r.raw_names,
                    "snippet": r.snippet,
                }
                for r in kept
            ],
            handle,
            indent=2,
        )

    known = [r for r in kept if r.year != "Unknown"]
    by_confidence: Dict[str, int] = {}
    for record in kept:
        by_confidence[record.confidence] = by_confidence.get(record.confidence, 0) + 1

    print(f"Rows: {len(kept)} (known year: {len(known)}, unknown: {len(kept) - len(known)})")
    if known:
        years = [int(r.year) for r in known]
        print(f"Year range: {min(years)}-{max(years)}")
    print(f"Confidence: {by_confidence}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {txt_path}")
    print(f"Wrote {json_path}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def crop_arg(value: str) -> tuple:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("crop must be LEFT,TOP,RIGHT_INSET,BOTTOM_INSET")
    try:
        return tuple(int(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("crop values must be integers") from exc


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process page images downloaded by canadiana_crawler.py.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    organize = subparsers.add_parser(
        "organize", help="Split downloaded images into originals and a working copy"
    )
    organize.add_argument("--pages-dir", default="c3026_pages", help="Directory holding downloaded images (default: %(default)s)")
    organize.add_argument("--originals-name", default="original_jpgs", help="Subfolder for pristine originals (default: %(default)s)")
    organize.add_argument("--conversions-name", default="pdf_conversions", help="Subfolder for working copies (default: %(default)s)")
    organize.add_argument("--no-copy", action="store_true", help="Only move originals; skip the working copy")
    organize.add_argument("--overwrite", action="store_true", help="Re-copy files that already exist")
    organize.set_defaults(func=cmd_organize)

    pdf = subparsers.add_parser("pdf", help="Combine page images into a single PDF")
    pdf.add_argument("--source-dir", default="c3026_pages/pdf_conversions", help="Directory of page images (default: %(default)s)")
    pdf.add_argument("--output", default=None, help="Destination PDF (default: <source-dir>/combined.pdf)")
    pdf.set_defaults(func=cmd_pdf)

    verify = subparsers.add_parser("verify-pdf", help="Check PDF structure and page count")
    verify.add_argument("--pdf", required=True, help="PDF file to verify")
    verify.add_argument("--source-dir", default=None, help="Compare page count against images in this directory")
    verify.add_argument("--expect-pages", type=int, default=None, help="Expected page count")
    verify.set_defaults(func=cmd_verify_pdf)

    ocr = subparsers.add_parser("ocr", help="Preprocess page images and OCR them to text files")
    ocr.add_argument("--source-dir", default="c3026_pages/original_jpgs", help="Directory of page images (default: %(default)s)")
    ocr.add_argument("--out-dir", default="ocr_text", help="Destination for per-page text; avoid /tmp (default: %(default)s)")
    ocr.add_argument("--start", type=int, default=1, help="First page number (default: %(default)s)")
    ocr.add_argument("--end", type=int, default=None, help="Last page number (inclusive)")
    ocr.add_argument("--crop", type=crop_arg, default=DEFAULT_CROP, metavar="L,T,R,B", help="Crop box as LEFT,TOP,RIGHT_INSET,BOTTOM_INSET (default: 760,1080,650,1280)")
    ocr.add_argument("--scale", type=int, default=DEFAULT_SCALE, help="Upscale factor before OCR (default: %(default)s)")
    ocr.add_argument("--contrast", type=float, default=DEFAULT_CONTRAST, help="Contrast boost (default: %(default)s)")
    ocr.add_argument("--psm", type=int, default=DEFAULT_PSM, help="Tesseract page segmentation mode (default: %(default)s)")
    ocr.add_argument("--keep-images", action="store_true", help="Keep preprocessed PNGs for inspection")
    ocr.add_argument("--overwrite", action="store_true", help="Re-OCR pages that already have text files")
    ocr.set_defaults(func=cmd_ocr)

    extract = subparsers.add_parser("extract", help="Parse OCR text into baptism records")
    extract.add_argument("--ocr-dir", default="ocr_text", help="Directory of page_NNNN.txt files (default: %(default)s)")
    extract.add_argument("--start", type=int, default=1, help="First page number to parse (default: %(default)s)")
    extract.add_argument("--end", type=int, default=None, help="Last page number to parse (inclusive)")
    extract.add_argument("--min-year", type=int, default=1785, help="Discard rows before this year (default: %(default)s)")
    extract.add_argument("--max-year", type=int, default=1813, help="Discard rows after this year (default: %(default)s)")
    extract.add_argument("--require-year", action="store_true", help="Drop rows with an unreadable year")
    extract.add_argument("--out-prefix", default="baptism_entries", help="Output path prefix; writes .csv/.txt/.json (default: %(default)s)")
    extract.set_defaults(func=cmd_extract)

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
