#!/usr/bin/env python3
"""
Download and cache the static JLC catalog published by the jlcparts project.

jlcparts data format v4 (2026): a single ``data/manifest.json`` lists merged
categories, each pointing at one or more ``browse-components-*.jsonl.gz``
shards (schema header line + one row per part, stock included), with part
attributes stored as indexes into a shared ``attributes-lut.json.gz``.

This module downloads those files and normalizes them into the same local
cache layout the rest of kicadsync reads:

  normalized/<slug>.json = {
    "category": "...",
    "subcategory": "...",
    "fetched_at": "...",
    "schema": [...],
    "parts": [{...}, {...}]        # attributes expanded from the LUT
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BASE_URL = "https://yaqwsx.github.io/jlcparts/data"
MANIFEST_URL = f"{BASE_URL}/manifest.json"
ATTRIBUTES_LUT_NAME = "attributes-lut.json.gz"
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
    """First scalar of an attribute value, e.g. 'Basic' from Basic/Extended.

    Handles both the legacy shape (values keyed by 'default') and the v4 LUT
    shape (values keyed by the attribute's 'primary' name).
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    values = value.get("values")
    if not isinstance(values, dict):
        return None
    keys = ["default"]
    primary = value.get("primary")
    if isinstance(primary, str):
        keys.append(primary)
    for key in keys:
        scalar_list = values.get(key)
        if isinstance(scalar_list, list) and scalar_list and isinstance(scalar_list[0], str):
            return scalar_list[0]
    return None


def parse_browse_shard(
    text: str,
    attributes_lut: Sequence[Any],
    category: str,
    subcategory: str,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Parse one browse-components jsonl payload into (schema, parts).

    Line 0 maps field name -> column index; the rest are rows. Attribute
    indexes are expanded through the shared LUT into the legacy
    {name: {format, primary, values}} dict shape.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return [], []
    header = json.loads(lines[0])
    schema = sorted(header, key=header.get)
    parts: List[Dict[str, Any]] = []
    for line in lines[1:]:
        row = json.loads(line)
        part = {name: row[idx] for name, idx in header.items() if idx < len(row)}
        attributes: Dict[str, Any] = {}
        raw_attributes = part.get("attributes")
        if isinstance(raw_attributes, list):
            for lut_index in raw_attributes:
                if isinstance(lut_index, int) and 0 <= lut_index < len(attributes_lut):
                    entry = attributes_lut[lut_index]
                    if isinstance(entry, list) and len(entry) == 2:
                        attributes[entry[0]] = entry[1]
        part["attributes"] = attributes
        part["category"] = category
        part["subcategory"] = subcategory
        parts.append(part)
    return schema, parts


@dataclass(frozen=True)
class CategoryRef:
    category: str
    subcategory: str
    shards: Tuple[str, ...]

    @property
    def slug(self) -> str:
        # "browse-components-<slug>-001.jsonl.gz" -> "<slug>"
        name = self.shards[0]
        for prefix in ("browse-components-",):
            if name.startswith(prefix):
                name = name[len(prefix):]
        if name.endswith(".jsonl.gz"):
            name = name[: -len(".jsonl.gz")]
        return name.rsplit("-", 1)[0] if name.rsplit("-", 1)[-1].isdigit() else name

    def shard_url(self, shard: str) -> str:
        return f"{BASE_URL}/{shard}"


class JlcCatalogCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.manifest_path = cache_dir / "manifest.json"
        self.raw_dir = cache_dir / "raw"
        self.normalized_dir = cache_dir / "normalized"
        self._attributes_lut: Optional[List[Any]] = None

    def fetch_manifest(self) -> Dict[str, Any]:
        data = json.loads(_fetch_bytes(MANIFEST_URL).decode("utf-8"))
        _write_json(self.manifest_path, data)
        return data

    def load_manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return self.fetch_manifest()
        return _load_json(self.manifest_path)

    def attributes_lut(self, force: bool = False) -> List[Any]:
        if self._attributes_lut is not None and not force:
            return self._attributes_lut
        lut_path = self.raw_dir / ATTRIBUTES_LUT_NAME
        if force or not lut_path.exists():
            _write_bytes(lut_path, _fetch_bytes(f"{BASE_URL}/{ATTRIBUTES_LUT_NAME}"))
        with gzip.open(lut_path, "rt", encoding="utf-8") as handle:
            self._attributes_lut = json.load(handle)
        return self._attributes_lut

    def list_categories(self) -> List[CategoryRef]:
        manifest = self.load_manifest()
        refs: List[CategoryRef] = []
        for entry in manifest["categories"]:
            shards = tuple(entry.get("browseShards", []))
            if not shards:
                continue
            refs.append(
                CategoryRef(
                    category=str(entry["category"]),
                    subcategory=str(entry.get("subcategory", "")),
                    shards=shards,
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
        normalized_path = self.normalized_dir / f"{ref.slug}.json"
        if not force and normalized_path.exists():
            return _load_json(normalized_path)

        lut = self.attributes_lut()
        schema: List[str] = []
        parts: List[Dict[str, Any]] = []
        for shard in ref.shards:
            raw_path = self.raw_dir / shard
            if force or not raw_path.exists():
                _write_bytes(raw_path, _fetch_bytes(ref.shard_url(shard)))
            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                shard_schema, shard_parts = parse_browse_shard(
                    handle.read(), lut, ref.category, ref.subcategory
                )
            schema = schema or shard_schema
            parts.extend(shard_parts)

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
        if force:
            self.fetch_manifest()
        refs = self.list_categories()
        if limit is not None:
            refs = refs[:limit]
        self.attributes_lut(force=force)
        total = len(refs)
        for index, ref in enumerate(refs, start=1):
            print(f"[{index}/{total}] {ref.category} / {ref.subcategory}")
            self.fetch_category(ref, force=force)
        if limit is None:
            self._purge_stale_files(refs)
        return total

    def _purge_stale_files(self, refs: Sequence[CategoryRef]) -> None:
        """Drop cache files that are not part of the current manifest.

        This clears out payloads from the pre-2026 data format (and any
        categories jlcparts has since removed) so reports never match against
        stale prices/stock."""
        expected_raw = {shard for ref in refs for shard in ref.shards} | {ATTRIBUTES_LUT_NAME}
        expected_normalized = {f"{ref.slug}.json" for ref in refs}
        removed = 0
        for path in self.raw_dir.glob("*"):
            if path.is_file() and path.name not in expected_raw:
                path.unlink()
                removed += 1
        for path in self.normalized_dir.glob("*"):
            if path.is_file() and path.name not in expected_normalized:
                path.unlink()
                removed += 1
        for legacy in (self.cache_dir / "stock", ):
            if legacy.is_dir():
                removed += sum(1 for p in legacy.rglob("*") if p.is_file())
                shutil.rmtree(legacy)
        legacy_index = self.cache_dir / "index.json"
        if legacy_index.exists():
            legacy_index.unlink()
            removed += 1
        if removed:
            print(f"[fetch-all] removed {removed} stale cache file(s) from previous catalog formats")

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
    manifest = cache.fetch_manifest()
    print(
        f"Saved manifest with {len(manifest['categories'])} categories "
        f"({manifest.get('totalComponents', '?')} components, "
        f"built {manifest.get('created', '?')}) to {cache.manifest_path}"
    )
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

    refresh_index = subparsers.add_parser("refresh-index", help="Download the latest catalog manifest")
    refresh_index.set_defaults(func=_cmd_refresh_index)

    list_categories = subparsers.add_parser("list-categories", help="Print category/subcategory pairs")
    list_categories.set_defaults(func=_cmd_list_categories)

    fetch_category = subparsers.add_parser("fetch-category", help="Download one category payload")
    fetch_category.add_argument("category", help="Exact category name from list-categories")
    fetch_category.add_argument("subcategory", help="Exact subcategory name from list-categories")
    fetch_category.add_argument("--force", action="store_true", help="Re-download even if cached")
    fetch_category.set_defaults(func=_cmd_fetch_category)

    fetch_all = subparsers.add_parser(
        "fetch-all",
        help="Download every category payload (--force also refreshes the manifest and prunes stale files)",
    )
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
