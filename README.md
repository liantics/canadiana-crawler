# Canadiana Directory Crawler
Small Python CLI app to crawl a Canadiana/Héritage directory page and download all page images to a local folder.

## Overview
This project targets pages like:

- `https://heritage.canadiana.ca/view/oocihm.lac_reel_c3026/`

It parses the viewer page, discovers all image entries, converts each IIIF `info.json` URL to a downloadable JPEG URL, and saves images as sequential files such as `page_0001.jpg`.

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

`requirements.txt` is included for standard virtual environment workflows. The crawler itself uses Python standard library modules only.

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

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
```bash
python3 canadiana_crawler.py --help
```

## Output
Downloaded files are written to `--output-dir` and named like:

- `page_0001.jpg`
- `page_0002.jpg`
- ...
