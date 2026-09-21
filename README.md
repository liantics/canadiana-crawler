# Canadiana Directory Crawler
Python CLI tools to download all page images from a Canadiana/Héritage directory page and turn them into a searchable PDF and extracted records.

## Overview
This project targets pages like:

- `https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/`

It parses the viewer page, discovers all image entries, converts each IIIF `info.json` URL to a downloadable JPEG URL, and saves images as sequential files such as `page_0001.jpg`.

There are two scripts:

- `canadiana_crawler.py` - downloads the page images
- `process_pages.py` - organizes them, builds a PDF, runs OCR, and extracts records

## Features
- Automatic page discovery from the directory viewer HTML
- Parallel downloads (`--workers`)
- Retry with backoff (`--retries`)
- Resume-safe behavior (skips existing files by default)
- Optional re-download mode (`--overwrite`)
- Page range support (`--start`, `--end`)
- Dry-run mode (`--dry-run`)

## Requirements
- Python 3.9+
- Internet access

`requirements.txt` is included for standard virtual environment workflows. The crawler itself uses Python standard library modules only; the `process_pages.py` pipeline needs a few extra packages, listed under [Processing the downloaded data](#processing-the-downloaded-data).

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Quick start
The full pipeline, from empty directory to extracted records:

```bash
# 1. Download every page image
python3 canadiana_crawler.py https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/ --output-dir c3026_pages --workers 8

# 2. Separate pristine originals from working copies
python3 process_pages.py organize --pages-dir c3026_pages

# 3. Build a single PDF and confirm the page count
python3 process_pages.py pdf --source-dir c3026_pages/pdf_conversions --output c3026_pages/pdf_conversions/c3026_combined.pdf
python3 process_pages.py verify-pdf --pdf c3026_pages/pdf_conversions/c3026_combined.pdf --source-dir c3026_pages/original_jpgs

# 4. OCR the pages, then extract records
python3 process_pages.py ocr --source-dir c3026_pages/original_jpgs --out-dir ocr_text
python3 process_pages.py extract --ocr-dir ocr_text --out-prefix baptism_entries
```

Steps 2-4 need the extra packages listed under [Additional tools](#additional-tools). Each step is explained in [Processing the downloaded data](#processing-the-downloaded-data).

## Usage examples

### 1) Dry run (discover pages, no downloads)
```bash
python3 canadiana_crawler.py https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/ --dry-run
```

### 2) Download all pages
```bash
python3 canadiana_crawler.py https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/ --output-dir c3026_pages
```

### 3) Download a specific range
```bash
python3 canadiana_crawler.py https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/ --start 100 --end 250 --output-dir c3026_pages
```

### 4) Use more workers and overwrite existing files
```bash
python3 canadiana_crawler.py https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/ --workers 8 --overwrite --output-dir c3026_pages
```

## CLI reference
Every flag for both tools is discoverable from the command line:

```bash
python3 canadiana_crawler.py --help
python3 process_pages.py --help
python3 process_pages.py <subcommand> --help
```

## Output
Downloaded files are written to `--output-dir` and named like:

- `page_0001.jpg`
- `page_0002.jpg`
- ...

Filenames are zero-padded to the width of the total page count, so a 1,732-page reel produces `page_0001.jpg` through `page_1732.jpg`.

## Processing the downloaded data
The crawler only fetches images. `process_pages.py` takes it from there, turning those images into a single PDF and then into extracted records.

```bash
python3 process_pages.py --help
```

Subcommands:

- `organize` - split downloaded images into originals and a working copy
- `pdf` - combine page images into a single PDF
- `verify-pdf` - check PDF structure and page count
- `ocr` - preprocess page images and OCR them to per-page text files
- `extract` - parse OCR text into baptism records

### Additional tools
The crawler itself needs no third-party packages, but processing does:

```bash
pip install Pillow img2pdf pikepdf
brew install tesseract   # local OCR engine (macOS)
```

All commands below assume you are in the directory that contains `c3026_pages` (the parent of `--output-dir`).

### 1) Organize the images
Keep pristine originals separate from anything you convert or preprocess. This moves `page_*.jpg` into `original_jpgs/` and copies them to `pdf_conversions/`:

```bash
python3 process_pages.py organize --pages-dir c3026_pages
```

Re-running is safe: files already in place are left alone.

### 2) Combine the pages into one PDF
```bash
python3 process_pages.py pdf \
  --source-dir c3026_pages/pdf_conversions \
  --output c3026_pages/pdf_conversions/c3026_combined.pdf
```

Verify the result before relying on it. Passing `--source-dir` compares the PDF page count against the number of source images and exits non-zero on a mismatch:

```bash
python3 process_pages.py verify-pdf \
  --pdf c3026_pages/pdf_conversions/c3026_combined.pdf \
  --source-dir c3026_pages/original_jpgs
```

### 3) OCR the pages
OCR runs against the source JPGs rather than the PDF. Each page is cropped to drop the microfilm leader and the archive title card, then upscaled and contrast-boosted before Tesseract runs:

```bash
python3 process_pages.py ocr \
  --source-dir c3026_pages/original_jpgs \
  --out-dir ocr_text \
  --start 557 --end 664
```

This writes one `page_NNNN.txt` per page. Pages that already have text are skipped unless you pass `--overwrite`. Tune the preprocessing with `--crop L,T,R,B`, `--scale`, `--contrast`, and `--psm`, and add `--keep-images` to inspect the preprocessed PNGs.

Caveats worth knowing before you trust the output:

- These registers are 18th/19th-century handwriting on microfilm. Local Tesseract handles the typed archive cards well but produces largely unusable text on the handwritten entries.
- The committed tables came from a higher-quality hosted OCR pass, not from `ocr`. Public demo API keys for such services are heavily rate limited, so expect HTTP 429 responses and plan to throttle, retry, and downscale images to satisfy upload size limits.
- On some macOS setups Tesseract cannot read images under `/tmp`; keep `--out-dir` elsewhere.
- Treat every extracted name and year as approximate until spot-checked against the page image.

### 4) Extract records
`extract` scans OCR text for baptism markers and child/parent cues, pulls the nearest four-digit year, normalizes noisy name tokens against a list of period-common given names, and labels each row `high`, `medium`, or `low` confidence.

```bash
python3 process_pages.py extract \
  --ocr-dir ocr_text \
  --start 558 --end 664 \
  --out-prefix st_georges_baptism_entries
```

That writes `.csv`, `.txt`, and `.json` alongside the given prefix. Rows outside `--min-year`/`--max-year` (default 1785-1813) are dropped, and `--require-year` discards rows whose year could not be read.

Because `--ocr-dir` accepts any directory of `page_NNNN.txt` files, you can point it at output from a better OCR engine without changing the parser.

Committed outputs for the Sydney St. George's Anglican register section (pages 557-664 of reel C-3026):

- `st_georges_baptism_entries_table_final.csv` - year, page, names, confidence
- `st_georges_baptism_entries_table_final.txt` - same data, tab-separated
- `st_georges_baptism_entries_normalized.json` - structured rows including raw name tokens and the OCR snippet each row came from

Use the `snippet` field in the JSON to trace any row back to its source text, and the `page` column to open the matching `page_NNNN.jpg` for visual confirmation.

See [Quick start](#quick-start) for the whole pipeline as a single copy-paste block.

Intermediate artifacts (preprocessed images, per-page OCR text, superseded extraction passes) are gitignored; only the final outputs are tracked.
