#!/usr/bin/env python3
"""
Report JLC price and stock for parts used in a schematic JSON.

The script reads a design JSON shaped like `schema.json`, searches the local
`jlc_data/cache/normalized` catalog for exact JLC/LCSC or MPN matches, and
prints two BOM views:
  1. Sorted by highest unit price first
  2. Sorted by lowest inventory first

Matching rules:
  - Prefer an exact `LCSC` match when present
  - Fall back to `mpn` when `LCSC` is absent or not found

Hierarchy rules:
  - `root_components` count once each
  - each `sheet_definitions[].components[]` entry is multiplied by the number of
    `sheet_instances` referencing that `sheet_id`
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


from .paths import jlc_cache_dir

DEFAULT_CACHE_DIR = jlc_cache_dir()
DEFAULT_NORMALIZED_DIR = DEFAULT_CACHE_DIR / "normalized"

_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE_RE = re.compile(r"//.*?$", re.M)


def strip_json_comments(text: str) -> str:
    text = _COMMENT_BLOCK_RE.sub("", text)
    text = _COMMENT_LINE_RE.sub("", text)
    return text


def normalize_part_key(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def format_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"${value:.4f}"


def format_inventory(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return str(value)


def attribute_default_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    values = value.get("values")
    if not isinstance(values, dict):
        return None
    # Legacy payloads key the scalar under "default"; the 2026 jlcparts
    # format keys it under the attribute's own "primary" name.
    keys = ["default"]
    primary = value.get("primary")
    if isinstance(primary, str):
        keys.append(primary)
    for key in keys:
        scalar_list = values.get(key)
        if isinstance(scalar_list, list) and scalar_list and isinstance(scalar_list[0], str):
            return scalar_list[0]
    return None


@dataclass
class Occurrence:
    ref: str
    mpn: str
    lcsc: str
    description: str
    footprint: str


@dataclass
class DesignGroup:
    schematic_mpn: str
    schematic_lcsc: str
    description: str
    occurrences: List[Occurrence] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.occurrences)

    @property
    def lookup_candidates(self) -> List[Tuple[str, str]]:
        candidates: List[Tuple[str, str]] = []
        normalized_lcsc = normalize_part_key(self.schematic_lcsc)
        if normalized_lcsc:
            candidates.append(("lcsc", normalized_lcsc))
        normalized_mpn = normalize_part_key(self.schematic_mpn)
        if normalized_mpn:
            candidates.append(("mpn", normalized_mpn))
        return candidates

    @property
    def example_refs(self) -> str:
        refs = [item.ref for item in self.occurrences[:5]]
        suffix = "" if len(self.occurrences) <= 5 else ", ..."
        return ", ".join(refs) + suffix

    @property
    def footprint_status(self) -> str:
        footprint_count = sum(1 for item in self.occurrences if item.footprint.strip())
        if footprint_count == 0:
            return "no"
        if footprint_count == len(self.occurrences):
            return "yes"
        return "mixed"


@dataclass
class MatchCandidate:
    lcsc: str
    mfr: str
    description: str
    stock: Optional[int]
    price_breaks: List[Dict[str, Any]]
    category: str
    subcategory: str
    library_type: Optional[str]
    source_file: Path


@dataclass
class ReportRow:
    schematic_mpn: str
    schematic_lcsc: str
    matched_mpn: str
    lcsc: str
    description: str
    quantity: int
    stock: Optional[int]
    unit_price: Optional[float]
    extended_price: Optional[float]
    library_type: Optional[str]
    category: str
    subcategory: str
    footprint_status: str


def load_design(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return json.loads(strip_json_comments(raw))


def collect_design_groups(design: Dict[str, Any]) -> List[DesignGroup]:
    grouped: Dict[str, DesignGroup] = {}

    def add_occurrence(ref: str, component: Dict[str, Any]) -> None:
        mpn = str(component.get("mpn", "")).strip()
        lcsc = str(component.get("LCSC", "")).strip()
        if not mpn and not lcsc:
            return
        description = str(component.get("description", ""))
        footprint = str(component.get("footprint", ""))
        group_key = f"lcsc:{normalize_part_key(lcsc)}" if lcsc else f"mpn:{normalize_part_key(mpn)}"
        group = grouped.get(group_key)
        if group is None:
            group = DesignGroup(
                schematic_mpn=mpn,
                schematic_lcsc=lcsc,
                description=description,
            )
            grouped[group_key] = group
        elif description and not group.description:
            group.description = description
        group.occurrences.append(
            Occurrence(
                ref=ref,
                mpn=mpn,
                lcsc=lcsc,
                description=description,
                footprint=footprint,
            )
        )

    for component in design.get("root_components", []):
        add_occurrence(str(component["refdes"]), component)

    sheet_by_id = {
        str(sheet["sheet_id"]): sheet
        for sheet in design.get("sheet_definitions", [])
    }
    for instance in design.get("sheet_instances", []):
        instance_name = str(instance["instance_name"])
        sheet_id = str(instance["sheet_id"])
        sheet = sheet_by_id.get(sheet_id)
        if sheet is None:
            raise ValueError(f"Unknown sheet_id {sheet_id!r} referenced by instance {instance_name!r}")
        for component in sheet.get("components", []):
            add_occurrence(f"{instance_name}/{component['refdes']}", component)

    return sorted(grouped.values(), key=lambda item: item.schematic_mpn)


def choose_unit_price(price_breaks: Iterable[Dict[str, Any]], quantity: int) -> Optional[float]:
    normalized_breaks: List[Tuple[int, int, float]] = []
    for entry in price_breaks:
        try:
            q_from = int(entry["qFrom"])
            q_to = int(entry["qTo"])
            price = float(entry["price"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized_breaks.append((q_from, q_to, price))

    if not normalized_breaks:
        return None

    normalized_breaks.sort(key=lambda item: item[0])
    for q_from, q_to, price in normalized_breaks:
        if q_from <= quantity <= q_to:
            return price
    if quantity < normalized_breaks[0][0]:
        return normalized_breaks[0][2]
    return normalized_breaks[-1][2]


def candidate_score(candidate: MatchCandidate, quantity: int) -> Tuple[float, int, str]:
    unit_price = choose_unit_price(candidate.price_breaks, quantity)
    price_score = unit_price if unit_price is not None else math.inf
    stock_score = candidate.stock if candidate.stock is not None else -1
    return (price_score, -stock_score, candidate.lcsc)


def scan_catalog_for_targets(
    normalized_dir: Path,
    mpn_target_keys: Sequence[str],
    lcsc_target_keys: Sequence[str],
) -> Dict[Tuple[str, str], List[MatchCandidate]]:
    wanted_mpn = set(mpn_target_keys)
    wanted_lcsc = set(lcsc_target_keys)
    matches: Dict[Tuple[str, str], List[MatchCandidate]] = {
        ("mpn", key): [] for key in wanted_mpn
    }
    matches.update({("lcsc", key): [] for key in wanted_lcsc})

    for path in sorted(normalized_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        category = str(payload.get("category", ""))
        subcategory = str(payload.get("subcategory", ""))
        for part in payload.get("parts", []):
            mfr = str(part.get("mfr", ""))
            lcsc = str(part.get("lcsc", ""))
            norm_mpn = normalize_part_key(mfr)
            norm_lcsc = normalize_part_key(lcsc)
            if norm_mpn not in wanted_mpn and norm_lcsc not in wanted_lcsc:
                continue

            candidate = MatchCandidate(
                lcsc=lcsc,
                mfr=mfr,
                description=str(part.get("description", "")),
                stock=part.get("stock") if isinstance(part.get("stock"), int) else None,
                price_breaks=part.get("price") if isinstance(part.get("price"), list) else [],
                category=category,
                subcategory=subcategory,
                library_type=attribute_default_text(part.get("attributes", {}).get("Basic/Extended")),
                source_file=path,
            )
            if norm_lcsc in wanted_lcsc:
                matches[("lcsc", norm_lcsc)].append(candidate)
            if norm_mpn in wanted_mpn:
                matches[("mpn", norm_mpn)].append(candidate)
    return matches


def build_report_rows(
    groups: Sequence[DesignGroup],
    catalog_matches: Dict[Tuple[str, str], List[MatchCandidate]],
) -> Tuple[List[ReportRow], List[DesignGroup]]:
    found_rows: List[ReportRow] = []
    missing_groups: List[DesignGroup] = []

    for group in groups:
        chosen_candidate: Optional[MatchCandidate] = None
        for lookup_kind, normalized_key in group.lookup_candidates:
            candidates = catalog_matches.get((lookup_kind, normalized_key), [])
            if not candidates:
                continue
            chosen_candidate = min(candidates, key=lambda item: candidate_score(item, group.quantity))
            break

        if chosen_candidate is None:
            missing_groups.append(group)
            continue

        unit_price = choose_unit_price(chosen_candidate.price_breaks, group.quantity)
        found_rows.append(
            ReportRow(
                schematic_mpn=group.schematic_mpn,
                schematic_lcsc=group.schematic_lcsc,
                matched_mpn=chosen_candidate.mfr,
                lcsc=chosen_candidate.lcsc,
                description=group.description or chosen_candidate.description,
                quantity=group.quantity,
                stock=chosen_candidate.stock,
                unit_price=unit_price,
                extended_price=None if unit_price is None else unit_price * group.quantity,
                library_type=chosen_candidate.library_type,
                category=chosen_candidate.category,
                subcategory=chosen_candidate.subcategory,
                footprint_status=group.footprint_status,
            )
        )

    return found_rows, missing_groups


def print_table(title: str, rows: Sequence[ReportRow]) -> None:
    print(title)
    print("=" * len(title))
    if not rows:
        print("(none)")
        print()
        return

    headers = [
        "schematic_mpn",
        "lcsc",
        "qty",
        "footprint",
        "unit_price",
        "ext_price",
        "stock",
        "type",
        "description",
    ]
    table_rows = [
        [
            row.schematic_mpn,
            row.lcsc,
            str(row.quantity),
            row.footprint_status,
            format_money(row.unit_price),
            format_money(row.extended_price),
            format_inventory(row.stock),
            row.library_type or "-",
            row.description,
        ]
        for row in rows
    ]

    widths = [len(header) for header in headers]
    for row in table_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    print("  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in table_rows:
        print("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))
    print()


def print_missing(groups: Sequence[DesignGroup]) -> None:
    print("Missing From JLC Cache")
    print("======================")
    if not groups:
        print("(none)")
        print()
        return
    for group in groups:
        lcsc_text = f" lcsc={group.schematic_lcsc}" if group.schematic_lcsc else ""
        print(f"- qty={group.quantity} mpn={group.schematic_mpn}{lcsc_text} description={group.description}")
    print()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design_json", type=Path, help="Schematic JSON file, e.g. phased-array.json")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"JLC cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit printed rows in each sorted report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text tables",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    normalized_dir = args.cache_dir / "normalized"
    if not normalized_dir.is_dir():
        print(f"ERROR: normalized JLC cache directory not found: {normalized_dir}", file=sys.stderr)
        return 2

    design = load_design(args.design_json)
    groups = collect_design_groups(design)
    mpn_target_keys = sorted(
        {
            normalized
            for group in groups
            for lookup_kind, normalized in group.lookup_candidates
            if lookup_kind == "mpn"
        }
    )
    lcsc_target_keys = sorted(
        {
            normalized
            for group in groups
            for lookup_kind, normalized in group.lookup_candidates
            if lookup_kind == "lcsc"
        }
    )
    catalog_matches = scan_catalog_for_targets(normalized_dir, mpn_target_keys, lcsc_target_keys)
    found_rows, missing_groups = build_report_rows(groups, catalog_matches)

    price_sorted = sorted(
        found_rows,
        key=lambda row: (
            row.extended_price is None,
            -(row.extended_price or 0.0),
            row.schematic_mpn,
        ),
    )
    stock_sorted = sorted(
        found_rows,
        key=lambda row: (
            row.stock is None,
            row.stock if row.stock is not None else math.inf,
            row.schematic_mpn,
        ),
    )

    if args.limit is not None:
        price_sorted = price_sorted[: args.limit]
        stock_sorted = stock_sorted[: args.limit]

    if args.json:
        payload = {
            "design": str(args.design_json),
            "found_count": len(found_rows),
            "missing_count": len(missing_groups),
            "price_desc": [row.__dict__ for row in price_sorted],
            "stock_asc": [row.__dict__ for row in stock_sorted],
            "missing": [
                {
                    "schematic_mpn": group.schematic_mpn,
                    "schematic_lcsc": group.schematic_lcsc,
                    "quantity": group.quantity,
                    "description": group.description,
                }
                for group in missing_groups
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    total_extended = sum(row.extended_price or 0.0 for row in found_rows)
    print(f"Design: {args.design_json}")
    print(f"Matched groups: {len(found_rows)}")
    print(f"Missing groups: {len(missing_groups)}")
    print(f"Estimated extended cost at matched quantity tiers: {format_money(total_extended)}")
    print()

    print_table("Highest Price First", price_sorted)
    print_table("Lowest Inventory First", stock_sorted)
    print_missing(missing_groups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
