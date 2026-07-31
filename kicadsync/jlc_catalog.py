#!/usr/bin/env python3
"""
Download and cache the static JLC catalog published by the jlcparts project.

This avoids scraping HTML and keeps a local cache of:
  - data/index.json
  - per-category .json.gz payloads
  - per-category stock tables

The normalized category files are plain JSON objects shaped like:
{
  "category": "...",
  "subcategory": "...",
  "fetched_at": "...",
  "schema": [...],
  "parts": [{...}, {...}]
}
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BASE_URL = "https://yaqwsx.github.io/jlcparts/data"
INDEX_URL = f"{BASE_URL}/index.json"
from .paths import jlc_cache_dir

DEFAULT_CACHE_DIR = jlc_cache_dir()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fetch_bytes(url: str) -> bytes:
    with urlopen(url) as response:
        return response.read()


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_text(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _attribute_default_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    values = value.get("values")
    if not isinstance(values, dict):
        return None
    default_value = values.get("default")
    if not isinstance(default_value, list) or not default_value:
        return None
    scalar = default_value[0]
    return scalar if isinstance(scalar, str) else None


def _rows_to_parts(schema: List[str], rows: Iterable[List[Any]]) -> List[Dict[str, Any]]:
    return [dict(zip(schema, row)) for row in rows]


@dataclass(frozen=True)
class CategoryRef:
    category: str
    subcategory: str
    sourcename: str

    @property
    def data_url(self) -> str:
        return f"{BASE_URL}/{self.sourcename}.json.gz"

    @property
    def stock_url(self) -> str:
        return f"{BASE_URL}/{self.sourcename}.stock.json"


class JlcCatalogCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.index_path = cache_dir / "index.json"
        self.raw_dir = cache_dir / "raw"
        self.normalized_dir = cache_dir / "normalized"
        self.stock_dir = cache_dir / "stock"

    def fetch_index(self) -> Dict[str, Any]:
        data = json.loads(_fetch_bytes(INDEX_URL).decode("utf-8"))
        _write_json(self.index_path, data)
        return data

    def load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return self.fetch_index()
        return _load_json(self.index_path)

    def list_categories(self) -> List[CategoryRef]:
        index = self.load_index()
        refs: List[CategoryRef] = []
        for category, subcategories in index["categories"].items():
            for subcategory, meta in subcategories.items():
                refs.append(
                    CategoryRef(
                        category=category,
                        subcategory=subcategory,
                        sourcename=meta["sourcename"],
                    )
                )
        refs.sort(key=lambda item: (item.category, item.subcategory))
        return refs

    def resolve_category(self, category: str, subcategory: str) -> CategoryRef:
        for ref in self.list_categories():
            if ref.category == category and ref.subcategory == subcategory:
                return ref
        raise KeyError(f"Unknown category/subcategory: {category!r} / {subcategory!r}")

    def fetch_category(self, ref: CategoryRef, force: bool = False) -> Dict[str, Any]:
        raw_path = self.raw_dir / f"{ref.sourcename}.json.gz"
        normalized_path = self.normalized_dir / f"{ref.sourcename}.json"
        stock_path = self.stock_dir / f"{ref.sourcename}.stock.json"

        if force or not raw_path.exists():
            _write_bytes(raw_path, _fetch_bytes(ref.data_url))
        if force or not stock_path.exists():
            _write_bytes(stock_path, _fetch_bytes(ref.stock_url))

        with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)

        schema = payload["schema"]
        parts = _rows_to_parts(schema, payload["components"])
        stock = _load_json(stock_path)
        for part in parts:
            lcsc = part.get("lcsc")
            part["stock"] = stock.get(lcsc)
            part["category"] = ref.category
            part["subcategory"] = ref.subcategory

        normalized = {
            "category": ref.category,
            "subcategory": ref.subcategory,
            "fetched_at": _utc_now(),
            "schema": schema,
            "parts": parts,
        }
        _write_json(normalized_path, normalized)
        return normalized

    def fetch_all(self, limit: Optional[int] = None, force: bool = False) -> int:
        refs = self.list_categories()
        if limit is not None:
            refs = refs[:limit]
        total = len(refs)
        for index, ref in enumerate(refs, start=1):
            print(f"[{index}/{total}] {ref.category} / {ref.subcategory}")
            self.fetch_category(ref, force=force)
        return total

    def search_downloaded(self, mpn: str, limit: int = 20) -> List[Dict[str, Any]]:
        target = _normalize_text(mpn)
        matches: List[Dict[str, Any]] = []
        for path in sorted(self.normalized_dir.glob("*.json")):
            payload = _load_json(path)
            for part in payload.get("parts", []):
                candidate = _normalize_text(str(part.get("mfr", "")))
                if candidate != target:
                    continue
                matches.append(
                    {
                        "lcsc": part.get("lcsc"),
                        "mfr": part.get("mfr"),
                        "description": part.get("description"),
                        "library_type": _attribute_default_text(
                            part.get("attributes", {}).get("Basic/Extended")
                        ),
                        "category": part.get("category"),
                        "subcategory": part.get("subcategory"),
                        "stock": part.get("stock"),
                        "datasheet": part.get("datasheet"),
                    }
                )
                if len(matches) >= limit:
                    return matches
        return matches


def _cmd_refresh_index(args: argparse.Namespace) -> int:
    cache = JlcCatalogCache(args.cache_dir)
    index = cache.fetch_index()
    print(f"Saved index with {len(index['categories'])} top-level categories to {cache.index_path}")
    return 0


def _cmd_list_categories(args: argparse.Namespace) -> int:
    cache = JlcCatalogCache(args.cache_dir)
    refs = cache.list_categories()
    for ref in refs:
        print(f"{ref.category} :: {ref.subcategory}")
    print(f"\n{len(refs)} category/subcategory pairs")
    return 0


def _cmd_fetch_category(args: argparse.Namespace) -> int:
    cache = JlcCatalogCache(args.cache_dir)
    ref = cache.resolve_category(args.category, args.subcategory)
    normalized = cache.fetch_category(ref, force=args.force)
    print(
        f"Saved {len(normalized['parts'])} parts for "
        f"{ref.category} / {ref.subcategory} into {cache.normalized_dir}"
    )
    return 0


def _cmd_fetch_all(args: argparse.Namespace) -> int:
    cache = JlcCatalogCache(args.cache_dir)
    count = cache.fetch_all(limit=args.limit, force=args.force)
    print(f"Fetched {count} category payloads into {args.cache_dir}")
    return 0


def _cmd_search_mpn(args: argparse.Namespace) -> int:
    cache = JlcCatalogCache(args.cache_dir)
    matches = cache.search_downloaded(args.mpn, limit=args.limit)
    if not matches:
        print(
            "No matches in downloaded categories. Run `refresh-index` and "
            "`fetch-category` or `fetch-all` first."
        )
        return 1
    print(json.dumps(matches, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Directory for downloaded JLC catalog files (default: {DEFAULT_CACHE_DIR})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_index = subparsers.add_parser("refresh-index", help="Download the latest category index")
    refresh_index.set_defaults(func=_cmd_refresh_index)

    list_categories = subparsers.add_parser("list-categories", help="Print category/subcategory pairs")
    list_categories.set_defaults(func=_cmd_list_categories)

    fetch_category = subparsers.add_parser("fetch-category", help="Download one category payload")
    fetch_category.add_argument("category", help="Exact category name from list-categories")
    fetch_category.add_argument("subcategory", help="Exact subcategory name from list-categories")
    fetch_category.add_argument("--force", action="store_true", help="Re-download even if cached")
    fetch_category.set_defaults(func=_cmd_fetch_category)

    fetch_all = subparsers.add_parser("fetch-all", help="Download every category payload")
    fetch_all.add_argument("--limit", type=int, default=None, help="Only fetch the first N categories")
    fetch_all.add_argument("--force", action="store_true", help="Re-download even if cached")
    fetch_all.set_defaults(func=_cmd_fetch_all)

    search_mpn = subparsers.add_parser("search-mpn", help="Search exact MPN in downloaded categories")
    search_mpn.add_argument("mpn", help="Manufacturer part number to search for")
    search_mpn.add_argument("--limit", type=int, default=20, help="Maximum matches to print")
    search_mpn.set_defaults(func=_cmd_search_mpn)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (HTTPError, URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
