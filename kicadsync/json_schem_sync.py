#!/usr/bin/env python3
"""
JSON ⇄ KiCad schematic synchronizer with one-level hierarchy support.

JSON model:
  {
    "root_components": [...],
    "sheet_definitions": [
      {
        "sheet_id": "child_logic",
        "file": "child_logic.kicad_sch",
        "components": [...]
      }
    ],
    "sheet_instances": [
      {
        "instance_name": "LOGIC_A",
        "sheet_id": "child_logic",
        "pin_map": [
          {"name": "DATA_IN", "net": "MCU_DATA", "scope": "global"}
        ]
      }
    ]
  }

Connection format is canonical:
  {"number": "...", "name": "...", "net": "...", "scope": "..."}

If scope is omitted, global is assumed.
Use {"net": "__NC__"} on a pin to emit a KiCad no-connect marker instead of a net label.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .paths import libs_json_path
from .kicad_sch_model import KicadSchematic, child_lists, find_child, get_property_value
from .kicad_sch_sexp import as_text, atom, list_head, parse_one
from .kicad_sch_ops import (
    PinPosition,
    add_no_connect_for_pin,
    find_symbol_by_property,
    add_wire_and_label_for_pin,
    candidate_label_points_for_pin,
    find_symbol_by_reference,
    find_symbol_pin,
    get_symbol_property,
    infer_label_near_pin,
    intersheet_x_for_label,
    label_angle_for_pin_angle,
    label_justify_for_angle,
    pin_angle_for_facing,
    remove_stale_labels,
    set_symbol_lib_id,
    set_symbol_property,
    clone_or_update_lib_symbol,
)
import sexpdata  # for parsing .kicad_sym files


# -----------------------------
# 1. Data model
# -----------------------------


VALID_SCOPES = {"local", "global", "hierarchical"}
NC_NET_NAME = "__NC__"
CONNECTION_GRID_MM = 1.27
PIN_PITCH_MM = 2.54
NEW_COMPONENT_PLACEMENT_GAP_MM = 20.32


@dataclass
class PinConn:
    number: str
    name: str
    net: str
    scope: str = "global"


@dataclass
class Component:
    refdes: str
    mpn: str = ""
    lcsc: str = ""
    footprint: str = ""
    description: str = ""
    notes: str = ""
    dnp: bool = False
    connections: List[PinConn] = field(default_factory=list)


@dataclass
class SheetDefinition:
    sheet_id: str
    file: str
    components: List[Component] = field(default_factory=list)


@dataclass
class SheetPinMap:
    name: str
    net: str
    scope: str = "global"


@dataclass
class SheetInstance:
    instance_name: str
    sheet_id: str
    pin_map: List[SheetPinMap] = field(default_factory=list)


@dataclass
class DesignData:
    root_components: List[Component] = field(default_factory=list)
    sheet_definitions: List[SheetDefinition] = field(default_factory=list)
    sheet_instances: List[SheetInstance] = field(default_factory=list)


@dataclass
class SymbolPin:
    number: str
    name: str
    pin_type: str = "passive"


@dataclass
class ResolvedSymbol:
    lib_id: str
    reference: str
    name: str
    description: str = ""
    footprint: str = ""
    datasheet: str = "~"
    pins: List[SymbolPin] = field(default_factory=list)
    matched_from_library: bool = False


@dataclass
class RenderComponent:
    component: Component
    symbol: ResolvedSymbol
    instance_entries: List["SymbolInstanceEntry"] = field(default_factory=list)


@dataclass(frozen=True)
class SymbolInstanceEntry:
    path: str
    reference: str


@dataclass(frozen=True)
class SheetOccurrence:
    sheet_id: str
    instance_name: str
    sheet_uuid: str
    path: str
    page: str


# -----------------------------
# 2. JSON helpers
# -----------------------------


_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE_RE = re.compile(r"//.*?$", re.M)


def strip_json_comments(text: str) -> str:
    text = _COMMENT_BLOCK_RE.sub("", text)
    text = _COMMENT_LINE_RE.sub("", text)
    return text


def _normalize_scope(raw_scope: Optional[str]) -> str:
    scope = (raw_scope or "global").strip().lower()
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope '{raw_scope}'. Expected one of: {sorted(VALID_SCOPES)}")
    return scope


def _parse_connection(entry: Dict[str, Any]) -> PinConn:
    net = str(entry.get("net", ""))
    if not net:
        raise ValueError(f"Connection {entry.get('number')}/{entry.get('name')} must include a net name")
    return PinConn(
        number=str(entry["number"]),
        name=str(entry["name"]),
        net=net,
        scope=_normalize_scope(entry.get("scope")),
    )


def _parse_component(entry: Dict[str, Any]) -> Component:
    conns = [_parse_connection(c) for c in entry.get("connections", [])]
    return Component(
        refdes=str(entry["refdes"]),
        mpn=str(entry.get("mpn", "")),
        lcsc=str(entry.get("LCSC", "")),
        footprint=str(entry.get("footprint", "")),
        description=str(entry.get("description", "")),
        notes=str(entry.get("notes", "")),
        dnp=bool(entry.get("dnp", False)),
        connections=conns,
    )


def _component_to_json(comp: Component) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "refdes": comp.refdes,
        "description": comp.description,
        "connections": [
            {
                "number": c.number,
                "name": c.name,
                "net": c.net,
                **({"scope": c.scope} if c.scope != "global" else {}),
            }
            for c in comp.connections
        ],
        "notes": comp.notes,
    }
    if comp.mpn:
        out["mpn"] = comp.mpn
    if comp.lcsc:
        out["LCSC"] = comp.lcsc
    if comp.footprint:
        out["footprint"] = comp.footprint
    if comp.dnp:
        out["dnp"] = True
    return out


def _notes_indicate_dnp(notes: str) -> bool:
    return bool(re.search(r"\b(?:dnp|do not populate)\b", notes, re.IGNORECASE))


def _component_is_dnp(comp: Component) -> bool:
    if comp.dnp:
        return True
    # Backward compatibility for existing designs that used notes + no MPN to mean DNP.
    return not comp.mpn.strip() and _notes_indicate_dnp(comp.notes)


def _symbol_pin_pairs(symbol: ResolvedSymbol) -> List[Tuple[str, str]]:
    return [(pin.number, pin.name) for pin in symbol.pins]


def _pin_sort_key(pin_number: str) -> List[Tuple[int, Any]]:
    tokens = re.findall(r"\d+|[^\d]+", pin_number.upper())
    return [(0, int(token)) if token.isdigit() else (1, token) for token in tokens]


def _sorted_symbol_pins(pins: Sequence[SymbolPin]) -> List[SymbolPin]:
    return sorted(pins, key=lambda pin: _pin_sort_key(pin.number))


def _generated_symbol_for_component(comp: Component) -> ResolvedSymbol:
    return ResolvedSymbol(
        lib_id=f"generated:{comp.refdes}",
        reference=_get_ref_prefix(comp.refdes),
        name=comp.mpn or comp.refdes,
        description=comp.description,
        footprint=comp.footprint,
        pins=_sorted_symbol_pins([SymbolPin(number=conn.number, name=conn.name) for conn in comp.connections]),
    )


def _normalize_part_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def _local_lib_symbol_name(*candidates: str) -> str:
    for raw in candidates:
        text = (raw or "").strip()
        if text:
            return re.sub(r"[^A-Za-z0-9_.+-]+", "_", text)
    return "UNNAMED"


def _component_lookup_keys(comp: Component) -> List[str]:
    keys: List[str] = []
    for raw in (comp.mpn,):
        norm = _normalize_part_key(raw)
        if norm and norm not in keys:
            keys.append(norm)
    return keys


def _library_json_payload(text: str) -> str:
    array_start = re.search(r"(?m)^\[\s*$", text)
    if array_start:
        return text[array_start.start():]

    stripped = text.lstrip()
    if re.match(r"^\[\s*\{", stripped, re.S):
        return stripped

    raise ValueError("libs.json does not contain a JSON array payload")


def _load_library_symbols(libs_path: Path) -> List[ResolvedSymbol]:
    if not libs_path.exists():
        return []

    try:
        payload = _library_json_payload(libs_path.read_text(encoding="utf-8"))
        raw_symbols = json.loads(payload)
    except Exception as exc:
        print(f"[generate] WARNING: Could not parse {libs_path}: {exc}")
        return []

    symbols: List[ResolvedSymbol] = []
    for entry in raw_symbols:
        pins = [
            SymbolPin(
                number=str(pin.get("number", "")),
                name=str(pin.get("name", "")),
                pin_type=str(pin.get("type", "passive") or "passive"),
            )
            for pin in entry.get("pins", [])
            if str(pin.get("number", ""))
        ]
        symbols.append(
            ResolvedSymbol(
                lib_id=str(entry.get("lib_id", "")),
                reference=str(entry.get("reference", "")),
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                footprint=str(entry.get("footprint", "")),
                datasheet=str(entry.get("datasheet", "~") or "~"),
                pins=pins,
                matched_from_library=True,
            )
        )
    return [symbol for symbol in symbols if symbol.lib_id and symbol.name]


def _library_pins_compatible(comp: Component, symbol: ResolvedSymbol) -> bool:
    if not symbol.pins:
        return True

    symbol_pin_numbers = {pin.number for pin in symbol.pins}
    return all(conn.number in symbol_pin_numbers for conn in comp.connections)


def _match_strength(part_key: str, symbol_key: str) -> int:
    if not part_key or not symbol_key:
        return 0
    if part_key == symbol_key:
        return 300
    if len(symbol_key) >= 6 and part_key.startswith(symbol_key):
        return 200 + len(symbol_key)
    if len(part_key) >= 6 and symbol_key.startswith(part_key):
        return 100 + len(part_key)
    return 0


def _resolve_component_symbol(comp: Component, library_symbols: Sequence[ResolvedSymbol]) -> ResolvedSymbol:
    fallback = _generated_symbol_for_component(comp)
    lookup_keys = _component_lookup_keys(comp)
    if not lookup_keys or not library_symbols:
        return fallback

    best_symbol: Optional[ResolvedSymbol] = None
    best_score = -1
    for candidate in library_symbols:
        candidate_key = _normalize_part_key(candidate.name)
        if not candidate_key:
            continue
        match_score = max((_match_strength(key, candidate_key) for key in lookup_keys), default=0)
        if match_score <= 0 or not _library_pins_compatible(comp, candidate):
            continue

        if candidate.pins:
            pin_name_by_number = {pin.number: _normalize_part_key(pin.name) for pin in candidate.pins}
            pin_name_matches = sum(
                1
                for conn in comp.connections
                if pin_name_by_number.get(conn.number)
                and pin_name_by_number[conn.number] == _normalize_part_key(conn.name)
            )
            match_score += pin_name_matches * 10

        if match_score > best_score:
            best_score = match_score
            best_symbol = candidate

    if best_symbol is None:
        return fallback

    resolved_pins = [SymbolPin(number=pin.number, name=pin.name, pin_type=pin.pin_type) for pin in best_symbol.pins]
    if not resolved_pins:
        resolved_pins = _sorted_symbol_pins([SymbolPin(number=conn.number, name=conn.name) for conn in comp.connections])

    return ResolvedSymbol(
        # We currently emit a simplified cached symbol body, not an exact KiCad
        # standard-library copy, so keep the lib_id project-local to avoid
        # "doesn't match copy in library" warnings in ERC.
        lib_id=f"generated:{_local_lib_symbol_name(comp.mpn, best_symbol.name, comp.refdes)}",
        reference=best_symbol.reference or _get_ref_prefix(comp.refdes),
        name=best_symbol.name or comp.mpn or comp.refdes,
        description=best_symbol.description,
        footprint=comp.footprint or best_symbol.footprint,
        datasheet=best_symbol.datasheet or "~",
        pins=resolved_pins,
        matched_from_library=True,
    )


def _resolve_render_components(
    components: Sequence[Component],
    library_symbols: Sequence[ResolvedSymbol],
) -> List[RenderComponent]:
    rendered = [RenderComponent(component=comp, symbol=_resolve_component_symbol(comp, library_symbols)) for comp in components]

    inferred_pins_by_lib_id: Dict[str, List[SymbolPin]] = {}
    for item in rendered:
        if not item.symbol.matched_from_library or item.symbol.pins:
            continue

        pins = inferred_pins_by_lib_id.setdefault(item.symbol.lib_id, [])
        seen_numbers = {pin.number for pin in pins}
        for conn in item.component.connections:
            if conn.number in seen_numbers:
                continue
            pins.append(SymbolPin(number=conn.number, name=conn.name))
            seen_numbers.add(conn.number)

    for item in rendered:
        if item.symbol.matched_from_library and not item.symbol.pins:
            item.symbol = ResolvedSymbol(
                lib_id=item.symbol.lib_id,
                reference=item.symbol.reference,
                name=item.symbol.name,
                description=item.symbol.description,
                footprint=item.symbol.footprint,
                datasheet=item.symbol.datasheet,
                pins=_sorted_symbol_pins([
                    SymbolPin(number=pin.number, name=pin.name, pin_type=pin.pin_type)
                    for pin in inferred_pins_by_lib_id.get(item.symbol.lib_id, [])
                ]),
                matched_from_library=True,
            )

    return rendered


def design_from_json(path: Path) -> DesignData:
    raw_txt = path.read_text(encoding="utf-8")
    cleaned = strip_json_comments(raw_txt)
    data = json.loads(cleaned)

    if not isinstance(data, dict):
        raise ValueError("Top-level JSON must be an object with root_components, sheet_definitions, and sheet_instances.")

    root_components = [_parse_component(c) for c in data.get("root_components", [])]
    sheet_definitions = []
    for s in data.get("sheet_definitions", []):
        sheet_definitions.append(
            SheetDefinition(
                sheet_id=str(s["sheet_id"]),
                file=str(s["file"]),
                components=[_parse_component(c) for c in s.get("components", [])],
            )
        )

    sheet_instances = []
    for i in data.get("sheet_instances", []):
        pin_map = [
            SheetPinMap(
                name=str(p["name"]),
                net=str(p["net"]),
                scope=_normalize_scope(p.get("scope")),
            )
            for p in i.get("pin_map", [])
        ]
        sheet_instances.append(
            SheetInstance(
                instance_name=str(i["instance_name"]),
                sheet_id=str(i["sheet_id"]),
                pin_map=pin_map,
            )
        )

    return DesignData(
        root_components=root_components,
        sheet_definitions=sheet_definitions,
        sheet_instances=sheet_instances,
    )


def design_live_net_names(design: DesignData) -> Set[str]:
    """Every net name the JSON mentions anywhere (all scopes, all sheets).

    Hierarchical interface names are included: they appear in schematics as
    hierarchical labels and sheet pins. Any schematic label whose name is NOT
    in this set refers to a net that no longer exists in the design.
    """
    names: Set[str] = set()
    all_components = list(design.root_components)
    for sheet_def in design.sheet_definitions:
        all_components.extend(sheet_def.components)
    for comp in all_components:
        for conn in comp.connections:
            if conn.net and conn.net != NC_NET_NAME:
                names.add(conn.net)
    for inst in design.sheet_instances:
        for pin in inst.pin_map:
            if pin.net and pin.net != NC_NET_NAME:
                names.add(pin.net)
    return names


def lint_single_pin_nets(design: DesignData) -> List[Tuple[str, str]]:
    """Flag nets that connect to exactly one pin — the classic net-name typo.

    Works purely on the JSON (no kicad-cli needed). Counts respect sheet
    instancing: local nets are counted per definition (each instance is an
    identical copy), global nets accumulate one count per instance, and
    pin_map entries count as one connection on the parent net. Returns
    (net_name, message) pairs.
    """
    inst_count: Dict[str, int] = {}
    for inst in design.sheet_instances:
        inst_count[inst.sheet_id] = inst_count.get(inst.sheet_id, 0) + 1

    shared: Dict[str, int] = {}  # global-scope nets, whole-design namespace
    local: Dict[Tuple[str, str], int] = {}  # (sheet_id, net) per-instance namespace

    for comp in design.root_components:
        for conn in comp.connections:
            if conn.net and conn.net != NC_NET_NAME and conn.scope != "hierarchical":
                shared[conn.net] = shared.get(conn.net, 0) + 1

    for sheet_def in design.sheet_definitions:
        copies = inst_count.get(sheet_def.sheet_id, 0)
        if copies == 0:
            continue
        for comp in sheet_def.components:
            for conn in comp.connections:
                if not conn.net or conn.net == NC_NET_NAME:
                    continue
                if conn.scope == "hierarchical":
                    continue  # interface pin; connectivity comes from pin_map
                if conn.scope == "local":
                    key = (sheet_def.sheet_id, conn.net)
                    local[key] = local.get(key, 0) + 1
                else:
                    shared[conn.net] = shared.get(conn.net, 0) + copies

    for inst in design.sheet_instances:
        for pin in inst.pin_map:
            if pin.net and pin.net != NC_NET_NAME:
                shared[pin.net] = shared.get(pin.net, 0) + 1

    findings = [
        (net, f"SINGLE-PIN net: {net} (typo in a net name?)")
        for net, count in sorted(shared.items())
        if count == 1
    ]
    findings.extend(
        (net, f"SINGLE-PIN net: {net} in sheet '{sheet_id}' (typo in a net name?)")
        for (sheet_id, net), count in sorted(local.items())
        if count == 1
    )
    return findings


def design_to_json(design: DesignData, path: Path) -> None:
    out = {
        "root_components": [_component_to_json(c) for c in design.root_components],
        "sheet_definitions": [
            {
                "sheet_id": s.sheet_id,
                "file": s.file,
                "components": [_component_to_json(c) for c in s.components],
            }
            for s in design.sheet_definitions
        ],
        "sheet_instances": [
            {
                "instance_name": i.instance_name,
                "sheet_id": i.sheet_id,
                **(
                    {
                        "pin_map": [
                            {
                                "name": p.name,
                                "net": p.net,
                                **({"scope": p.scope} if p.scope != "global" else {}),
                            }
                            for p in i.pin_map
                        ]
                    }
                    if i.pin_map
                    else {}
                ),
            }
            for i in design.sheet_instances
        ],
    }
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")


# -----------------------------
# 3. KiCad 9 native helpers
# -----------------------------


def load_schematic(path: Path) -> KicadSchematic:
    return KicadSchematic.from_file(path)


def _find_symbol_for_component(schem: KicadSchematic, comp: Component) -> Optional[List[Any]]:
    sym = find_symbol_by_property(schem, "JsonRefdes", comp.refdes)
    if sym is not None:
        return sym
    return find_symbol_by_reference(schem, comp.refdes)


def _set_symbol_flag(symbol: List[Any], name: str, enabled: bool) -> None:
    flag_value = atom("yes" if enabled else "no")
    existing = find_child(symbol, name)
    if existing is not None and len(existing) >= 2 and not isinstance(existing[1], list):
        existing[1] = flag_value
        return
    symbol.append([atom(name), flag_value])


def _get_symbol_flag(symbol: List[Any], name: str, default: bool = False) -> bool:
    existing = find_child(symbol, name)
    if existing is None or len(existing) < 2 or isinstance(existing[1], list):
        return default
    return as_text(existing[1]).strip().lower() == "yes"


def _load_nearby_library_symbols(json_path: Path, sch_path: Path) -> List[ResolvedSymbol]:
    library_candidates = [
        json_path.parent / "libs.json",
        sch_path.parent / "libs.json",
        libs_json_path(),
    ]
    for candidate in library_candidates:
        if candidate.exists():
            symbols = _load_library_symbols(candidate)
            if symbols:
                print(f"[generate] Loaded {len(symbols)} symbols from {candidate}")
            return symbols
    return []


def _apply_components_to_schematic(
    components: Sequence[Component],
    sch_path: Path,
    out_path: Optional[Path],
    library_symbols: Sequence[ResolvedSymbol],
    ref_counters: Optional[Dict[str, int]] = None,
    live_net_names: Optional[Set[str]] = None,
) -> None:
    schem = load_schematic(sch_path)
    schem_symbols = schem.top_level_nodes("symbol")
    if not schem_symbols:
        if not components:
            print(f"[push] INFO: no symbols and no components requested for {sch_path.name}; skipping")
            return
        print(
            f"[push] ERROR: no symbols in {sch_path} (wanted to map {len(components)} components by refdes). "
            "Use 'generate' first or pre-place symbols in KiCad, then run 'push'."
        )
        return

    found_count = 0
    missing_refs: List[str] = []
    added_refs: List[str] = []
    removed_refs: List[str] = []
    pins_connected = 0
    desired_refdes = {comp.refdes for comp in components}

    for sym in list(schem.top_level_nodes("symbol")):
        json_refdes = get_property_value(sym, "JsonRefdes")
        if not json_refdes or json_refdes in desired_refdes:
            continue
        _remove_symbol_and_artifacts(schem, sym)
        removed_refs.append(json_refdes)

    for comp in components:
        sym = _find_symbol_for_component(schem, comp)
        if sym is None:
            sym = _add_missing_symbol_to_schematic(
                schem=schem,
                comp=comp,
                sch_path=sch_path,
                library_symbols=library_symbols,
                ref_counters=ref_counters,
            )
            if sym is None:
                missing_refs.append(comp.refdes)
                continue
            added_refs.append(comp.refdes)

        found_count += 1
        desired_symbol = _resolve_component_symbol(comp, library_symbols)
        current_lib_id = find_child(sym, "lib_id")
        current_lib_id_text = as_text(current_lib_id[1]) if current_lib_id and len(current_lib_id) >= 2 else ""
        if desired_symbol.lib_id and current_lib_id_text and desired_symbol.lib_id != current_lib_id_text:
            # A lib-symbol swap can move pins even when the pin list stays the same.
            # Remove old auto-generated stubs/labels before reconnecting the new geometry.
            _remove_symbol_pin_artifacts(schem, sym)
            if clone_or_update_lib_symbol(
                schem,
                current_lib_id_text,
                desired_symbol.lib_id,
                value=comp.mpn or desired_symbol.name,
                footprint=comp.footprint or desired_symbol.footprint,
                datasheet=desired_symbol.datasheet or "~",
                description=comp.description or desired_symbol.description,
                reference=desired_symbol.reference or _get_ref_prefix(comp.refdes),
            ):
                set_symbol_lib_id(sym, desired_symbol.lib_id)
        _refresh_generated_symbol_definition(schem, sym, desired_symbol)

        value_text = comp.mpn or desired_symbol.name
        footprint_text = comp.footprint or desired_symbol.footprint
        description_text = comp.description or desired_symbol.description
        datasheet_text = desired_symbol.datasheet or "~"

        dnp_enabled = _component_is_dnp(comp)
        _set_symbol_flag(sym, "in_bom", bool(comp.mpn.strip()) and not dnp_enabled)
        _set_symbol_flag(sym, "on_board", True)
        _set_symbol_flag(sym, "dnp", dnp_enabled)
        set_symbol_property(sym, "Value", value_text)
        set_symbol_property(sym, "JsonRefdes", comp.refdes)
        set_symbol_property(sym, "MPN", comp.mpn)
        set_symbol_property(sym, "LCSC", comp.lcsc)
        set_symbol_property(sym, "Footprint", footprint_text)
        set_symbol_property(sym, "Datasheet", datasheet_text)
        set_symbol_property(sym, "Description", description_text)
        set_symbol_property(sym, "Notes", comp.notes)

        for conn in comp.connections:
            try:
                pin = find_symbol_pin(sym, conn.number, conn.name, schem)
            except KeyError as exc:
                print(f"[push] WARNING: {exc}")
                continue
            if conn.net == NC_NET_NAME:
                added = add_no_connect_for_pin(schem=schem, pin=pin)
            else:
                added = add_wire_and_label_for_pin(
                    schem=schem,
                    pin=pin,
                    net_name=conn.net,
                    scope=_normalize_scope(conn.scope),
                    stub_len=2.54,
                )
            if added:
                pins_connected += 1

    if live_net_names is not None:
        for head, name in remove_stale_labels(schem, live_net_names):
            kind = head.replace("_", " ")
            print(f"[push] Removed stale {kind} '{name}' in {sch_path.name} (net not in JSON)")

    print(f"[push] {sch_path.name}: matched={found_count} pins_connected={pins_connected} missing={len(missing_refs)}")
    if added_refs:
        print(f"[push] Added refs in {sch_path.name}: {sorted(added_refs)}")
    if removed_refs:
        print(f"[push] Removed refs in {sch_path.name}: {sorted(removed_refs)}")
    if missing_refs:
        print(f"[push] Missing refs in {sch_path.name}: {sorted(missing_refs)}")
    target = out_path if out_path else sch_path
    schem.to_file(target)


def _find_lib_symbol_entry(schem: KicadSchematic, lib_id: str) -> Optional[List[Any]]:
    lib_symbols = schem.find_first("lib_symbols")
    if lib_symbols is None:
        return None
    for entry in child_lists(lib_symbols, "symbol"):
        if len(entry) >= 2 and as_text(entry[1]) == lib_id:
            return entry
    return None


def _ensure_lib_symbol_present(schem: KicadSchematic, symbol: ResolvedSymbol) -> None:
    if _find_lib_symbol_entry(schem, symbol.lib_id) is not None:
        return

    lib_symbols = schem.find_first("lib_symbols")
    if lib_symbols is None:
        lib_symbols = [atom("lib_symbols")]
        schem.root.insert(1, lib_symbols)

    lib_symbols.append(parse_one(_generate_lib_symbol(symbol)))


def _new_symbol_pin_node(pin_number: str) -> List[Any]:
    return parse_one(
        f'''(pin "{_escape_string(pin_number)}"
	(uuid "{_make_uuid()}"))'''
    )


def _sync_symbol_instance_pins(sym: List[Any], desired_symbol: ResolvedSymbol) -> None:
    existing_pin_nodes = child_lists(sym, "pin")
    existing_by_number: Dict[str, List[Any]] = {}
    for pin_node in existing_pin_nodes:
        if len(pin_node) >= 2 and not isinstance(pin_node[1], list):
            existing_by_number[as_text(pin_node[1])] = pin_node

    for pin_node in existing_pin_nodes:
        sym.remove(pin_node)

    instances_node = find_child(sym, "instances")
    insert_at = sym.index(instances_node) if instances_node is not None else len(sym)
    for offset, pin_number in enumerate(pin.number for pin in desired_symbol.pins):
        sym.insert(insert_at + offset, existing_by_number.get(pin_number) or _new_symbol_pin_node(pin_number))


def _refresh_generated_symbol_definition(
    schem: KicadSchematic,
    sym: List[Any],
    desired_symbol: ResolvedSymbol,
) -> None:
    if not desired_symbol.lib_id.startswith("generated:"):
        return

    existing_pin_numbers = [
        as_text(pin_node[1])
        for pin_node in child_lists(sym, "pin")
        if len(pin_node) >= 2 and not isinstance(pin_node[1], list)
    ]
    desired_pin_numbers = [pin.number for pin in desired_symbol.pins]
    if existing_pin_numbers != desired_pin_numbers:
        _remove_symbol_pin_artifacts(schem, sym)

    new_entry = parse_one(_generate_lib_symbol(desired_symbol).strip())
    if not isinstance(new_entry, list) or list_head(new_entry) != "symbol":
        return

    existing_entry = _find_lib_symbol_entry(schem, desired_symbol.lib_id)
    if existing_entry is None:
        _ensure_lib_symbol_present(schem, desired_symbol)
    else:
        existing_entry[:] = new_entry

    _sync_symbol_instance_pins(sym, desired_symbol)


def _symbol_instance_template(schem: KicadSchematic, sch_path: Path) -> Tuple[str, List[str]]:
    for sym in schem.top_level_nodes("symbol"):
        instances = find_child(sym, "instances")
        if instances is None:
            continue
        for project in child_lists(instances, "project"):
            if len(project) < 2 or isinstance(project[1], list):
                continue
            project_name = as_text(project[1]) or sch_path.stem
            paths = [
                as_text(path_node[1])
                for path_node in child_lists(project, "path")
                if len(path_node) >= 2 and not isinstance(path_node[1], list)
            ]
            if paths:
                return (project_name, paths)
    return (sch_path.stem, ["/"])


def _collect_reference_numbers_from_schematic(schem: KicadSchematic, ref_counters: Dict[str, int]) -> None:
    for sym in schem.top_level_nodes("symbol"):
        instances = find_child(sym, "instances")
        if instances is not None:
            for project in child_lists(instances, "project"):
                for path_node in child_lists(project, "path"):
                    reference_node = find_child(path_node, "reference")
                    if reference_node is None or len(reference_node) < 2 or isinstance(reference_node[1], list):
                        continue
                    reference = as_text(reference_node[1])
                    match = re.match(r"^([A-Z]+)(\d+)$", reference)
                    if match:
                        prefix = match.group(1)
                        ref_counters[prefix] = max(ref_counters.get(prefix, 0), int(match.group(2)))
        reference_prop = get_property_value(sym, "Reference") or ""
        match = re.match(r"^([A-Z]+)(\d+)$", reference_prop)
        if match:
            prefix = match.group(1)
            ref_counters[prefix] = max(ref_counters.get(prefix, 0), int(match.group(2)))


def _build_project_reference_counters(design: DesignData, root_sch_path: Path) -> Dict[str, int]:
    ref_counters: Dict[str, int] = {}
    schematics_to_scan = [root_sch_path]
    for sheet_def in design.sheet_definitions:
        child_path = root_sch_path.parent / sheet_def.file
        if child_path.exists():
            schematics_to_scan.append(child_path)

    seen_paths: Set[Path] = set()
    for sch_path in schematics_to_scan:
        resolved = sch_path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        _collect_reference_numbers_from_schematic(load_schematic(sch_path), ref_counters)
    return ref_counters


def _next_instance_entries_for_component(
    schem: KicadSchematic,
    comp: Component,
    paths: Sequence[str],
    ref_counters: Optional[Dict[str, int]] = None,
) -> List[SymbolInstanceEntry]:
    prefix = _get_ref_prefix(comp.refdes)
    if ref_counters is None:
        ref_counters = {}
        _collect_reference_numbers_from_schematic(schem, ref_counters)
    next_number = ref_counters.get(prefix, 0) + 1
    entries: List[SymbolInstanceEntry] = []
    for path in paths:
        entries.append(SymbolInstanceEntry(path=path, reference=f"{prefix}{next_number}"))
        next_number += 1
    ref_counters[prefix] = next_number - 1
    return entries


def _existing_symbol_right_edge(schem: KicadSchematic, sym: List[Any]) -> Optional[float]:
    right_edge: Optional[float] = None
    for pin_node in child_lists(sym, "pin"):
        if len(pin_node) < 2 or isinstance(pin_node[1], list):
            continue
        try:
            pin = find_symbol_pin(sym, as_text(pin_node[1]), "", schem)
        except KeyError:
            continue
        if pin.angle == 180.0:
            right_edge = max(right_edge or float("-inf"), pin.x - PIN_PITCH_MM)
        else:
            right_edge = max(right_edge or float("-inf"), pin.x + PIN_PITCH_MM)
    return right_edge


def _label_visual_right_edge(label: List[Any]) -> Optional[float]:
    if len(label) < 2 or isinstance(label[1], list):
        return None
    at = find_child(label, "at")
    if at is None or len(at) < 3:
        return None
    x = _parse_float_node(at[1])
    if x is None:
        return None
    angle = _parse_float_node(at[3]) if len(at) >= 4 else 0.0
    if angle is None:
        angle = 0.0
    if int(round(angle)) % 360 == 0:
        return x + _estimate_text_width(as_text(label[1]))
    return x


def _occupied_right_edge(schem: KicadSchematic) -> float:
    right_edge = 0.0

    for sym in schem.top_level_nodes("symbol"):
        at = find_child(sym, "at")
        if at is not None and len(at) >= 2:
            x = _parse_float_node(at[1])
            if x is not None:
                right_edge = max(right_edge, x)
        sym_right = _existing_symbol_right_edge(schem, sym)
        if sym_right is not None:
            right_edge = max(right_edge, sym_right)

    for wire in schem.top_level_nodes("wire"):
        pts = find_child(wire, "pts")
        if pts is None:
            continue
        for xy in child_lists(pts, "xy"):
            if len(xy) < 3:
                continue
            x = _parse_float_node(xy[1])
            if x is not None:
                right_edge = max(right_edge, x)

    for head in ("label", "hierarchical_label", "global_label", "no_connect"):
        for node in schem.top_level_nodes(head):
            if head == "no_connect":
                at = find_child(node, "at")
                if at is None or len(at) < 3:
                    continue
                x = _parse_float_node(at[1])
                if x is not None:
                    right_edge = max(right_edge, x)
                continue
            label_right = _label_visual_right_edge(node)
            if label_right is not None:
                right_edge = max(right_edge, label_right)

    return right_edge


def _choose_new_symbol_position(schem: KicadSchematic, item: RenderComponent) -> Tuple[float, float]:
    positions: List[Tuple[float, float]] = []
    for sym in schem.top_level_nodes("symbol"):
        at = find_child(sym, "at")
        if at is None or len(at) < 3:
            continue
        try:
            positions.append((float(as_text(at[1])), float(as_text(at[2]))))
        except (TypeError, ValueError):
            continue

    if not positions:
        return (50.0, 50.0)

    left_extent, _right_extent, _top_extent, _bottom_extent = _component_layout_metrics(item)
    max_x = _occupied_right_edge(schem)
    min_y = min(y for _, y in positions)
    # `left_extent` includes the new symbol body, pin stubs, and any left-side net labels.
    # Add a larger fixed gutter after existing content so late-added symbols do not crowd it.
    return (_snap_up(max_x + left_extent + NEW_COMPONENT_PLACEMENT_GAP_MM), _snap_to_grid(min_y))


def _add_missing_symbol_to_schematic(
    schem: KicadSchematic,
    comp: Component,
    sch_path: Path,
    library_symbols: Sequence[ResolvedSymbol],
    ref_counters: Optional[Dict[str, int]] = None,
) -> Optional[List[Any]]:
    desired_symbol = _resolve_component_symbol(comp, library_symbols)
    _ensure_lib_symbol_present(schem, desired_symbol)

    project_name, paths = _symbol_instance_template(schem, sch_path)
    item = RenderComponent(
        component=comp,
        symbol=desired_symbol,
        instance_entries=_next_instance_entries_for_component(schem, comp, paths, ref_counters=ref_counters),
    )
    x, y = _choose_new_symbol_position(schem, item)
    sym = parse_one(
        _generate_symbol_instance(
            item=item,
            x=x,
            y=y,
            project_name=project_name,
        ).strip()
    )
    if not isinstance(sym, list) or list_head(sym) != "symbol":
        return None

    schem.append_top_level(sym)
    return sym


def _parse_float_node(node: Any) -> Optional[float]:
    try:
        return float(as_text(node))
    except (TypeError, ValueError):
        return None


def _coords_close(a: float, b: float, eps: float = 0.02) -> bool:
    return abs(a - b) <= eps


def _wire_matches_points(
    wire: List[Any],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> bool:
    pts = find_child(wire, "pts")
    if pts is None:
        return False
    xy = child_lists(pts, "xy")
    if len(xy) != 2 or len(xy[0]) < 3 or len(xy[1]) < 3:
        return False

    ax = _parse_float_node(xy[0][1])
    ay = _parse_float_node(xy[0][2])
    bx = _parse_float_node(xy[1][1])
    by = _parse_float_node(xy[1][2])
    if None in {ax, ay, bx, by}:
        return False

    return (
        _coords_close(ax, x1)
        and _coords_close(ay, y1)
        and _coords_close(bx, x2)
        and _coords_close(by, y2)
    ) or (
        _coords_close(ax, x2)
        and _coords_close(ay, y2)
        and _coords_close(bx, x1)
        and _coords_close(by, y1)
    )


def _node_at_matches(node: List[Any], x: float, y: float) -> bool:
    at = find_child(node, "at")
    if at is None or len(at) < 3:
        return False
    node_x = _parse_float_node(at[1])
    node_y = _parse_float_node(at[2])
    if node_x is None or node_y is None:
        return False
    return _coords_close(node_x, x) and _coords_close(node_y, y)


def _remove_symbol_pin_artifacts(schem: KicadSchematic, sym: List[Any]) -> None:
    for pin_node in child_lists(sym, "pin"):
        if len(pin_node) < 2 or isinstance(pin_node[1], list):
            continue
        pin_number = as_text(pin_node[1])
        try:
            pin = find_symbol_pin(sym, pin_number, "", schem)
        except KeyError:
            continue

        for no_connect in list(schem.top_level_nodes("no_connect")):
            if _node_at_matches(no_connect, pin.x, pin.y):
                schem.remove_top_level(no_connect)

        for label_x, label_y in candidate_label_points_for_pin(pin.x, pin.y, pin.angle, 2.54):
            for head in ("label", "hierarchical_label", "global_label"):
                for label in list(schem.top_level_nodes(head)):
                    if _node_at_matches(label, label_x, label_y):
                        schem.remove_top_level(label)
            for wire in list(schem.top_level_nodes("wire")):
                if _wire_matches_points(wire, pin.x, pin.y, label_x, label_y):
                    schem.remove_top_level(wire)


def _remove_unused_lib_symbol_entry(schem: KicadSchematic, lib_id: str) -> None:
    if not lib_id:
        return
    for other_sym in schem.top_level_nodes("symbol"):
        other_lib_id = find_child(other_sym, "lib_id")
        if other_lib_id is not None and len(other_lib_id) >= 2 and as_text(other_lib_id[1]) == lib_id:
            return

    lib_symbols = schem.find_first("lib_symbols")
    if lib_symbols is None:
        return
    entry = _find_lib_symbol_entry(schem, lib_id)
    if entry is not None:
        lib_symbols.remove(entry)


def _remove_symbol_and_artifacts(schem: KicadSchematic, sym: List[Any]) -> None:
    lib_id = ""
    lib_id_node = find_child(sym, "lib_id")
    if lib_id_node is not None and len(lib_id_node) >= 2 and not isinstance(lib_id_node[1], list):
        lib_id = as_text(lib_id_node[1])

    _remove_symbol_pin_artifacts(schem, sym)
    schem.remove_top_level(sym)
    _remove_unused_lib_symbol_entry(schem, lib_id)


def _pull_components_from_schematic(
    old_components: Sequence[Component],
    sch_path: Path,
    search_radius: float,
) -> List[Component]:
    schem = load_schematic(sch_path)
    new_components: List[Component] = []

    for old_comp in old_components:
        sym = _find_symbol_for_component(schem, old_comp)
        if sym is None:
            print(f"[pull] WARNING: no symbol with refdes {old_comp.refdes} in {sch_path.name}; keeping entry")
            new_components.append(old_comp)
            continue

        refdes = get_symbol_property(sym, "JsonRefdes") or old_comp.refdes
        mpn = get_symbol_property(sym, "MPN") or old_comp.mpn
        lcsc = get_symbol_property(sym, "LCSC") or old_comp.lcsc
        footprint = get_symbol_property(sym, "Footprint") or old_comp.footprint
        description = get_symbol_property(sym, "Description") or old_comp.description
        notes = get_symbol_property(sym, "Notes") or old_comp.notes
        dnp = _get_symbol_flag(sym, "dnp", default=old_comp.dnp)
        if not dnp and not mpn.strip() and _notes_indicate_dnp(notes):
            dnp = True

        new_comp = Component(
            refdes=refdes,
            mpn=mpn,
            lcsc=lcsc,
            footprint=footprint,
            description=description,
            notes=notes,
            dnp=dnp,
            connections=[],
        )

        for conn in old_comp.connections:
            if conn.net == NC_NET_NAME:
                new_comp.connections.append(conn)
                continue
            try:
                pin = find_symbol_pin(sym, conn.number, conn.name, schem)
            except KeyError as exc:
                print(f"[pull] WARNING: {exc}")
                new_comp.connections.append(conn)
                continue

            nearby = infer_label_near_pin(schem, pin, search_radius=search_radius)
            if nearby is None:
                new_comp.connections.append(conn)
            else:
                new_comp.connections.append(
                    PinConn(
                        number=conn.number,
                        name=conn.name,
                        net=nearby.net,
                        scope=nearby.scope,
                    )
                )

        new_components.append(new_comp)

    return new_components


# -----------------------------
# 4. Push: JSON → KiCad
# -----------------------------


def _root_schematic_supports_incremental_push(design: DesignData, sch_path: Path) -> bool:
    if not sch_path.exists():
        return False

    schem = load_schematic(sch_path)
    if design.root_components and not schem.top_level_nodes("symbol"):
        return False
    if design.sheet_instances and not schem.top_level_nodes("sheet"):
        return False
    return True


def push_json_to_schematic(json_path: Path, sch_path: Path, out_path: Optional[Path] = None) -> None:
    design = design_from_json(json_path)
    root_target = out_path if out_path else sch_path
    if not _root_schematic_supports_incremental_push(design, sch_path):
        print(f"[push] Bootstrapping schematic from JSON into {root_target}")
        generate_schematic_from_json(json_path, root_target)
        return

    library_symbols = _load_nearby_library_symbols(json_path, sch_path)
    ref_counters = _build_project_reference_counters(design, sch_path)
    live_net_names = design_live_net_names(design)

    # Root sheet (optional alternate output path).
    _apply_components_to_schematic(
        design.root_components, sch_path, out_path, library_symbols, ref_counters, live_net_names
    )

    # Child sheet files are updated in place.
    sheet_by_id = {s.sheet_id: s for s in design.sheet_definitions}
    for instance in design.sheet_instances:
        if instance.sheet_id not in sheet_by_id:
            raise ValueError(f"sheet_instances references unknown sheet_id '{instance.sheet_id}'")

    for sheet_def in design.sheet_definitions:
        child_path = sch_path.parent / sheet_def.file
        if not child_path.exists():
            print(f"[push] WARNING: child schematic not found, skipping: {child_path}")
            continue
        _apply_components_to_schematic(
            sheet_def.components, child_path, None, library_symbols, ref_counters, live_net_names
        )


# -----------------------------
# 5. Pull: KiCad → JSON
# -----------------------------


def pull_schematic_to_json(
    json_in: Path,
    sch_path: Path,
    json_out: Optional[Path] = None,
    search_radius: float = 5.0,
) -> None:
    design = design_from_json(json_in)
    design.root_components = _pull_components_from_schematic(
        design.root_components, sch_path, search_radius
    )

    for sheet_def in design.sheet_definitions:
        child_path = sch_path.parent / sheet_def.file
        if not child_path.exists():
            print(f"[pull] WARNING: child schematic not found, keeping JSON entries: {child_path}")
            continue
        sheet_def.components = _pull_components_from_schematic(
            sheet_def.components, child_path, search_radius
        )

    json_target = json_out if json_out else json_in
    print(f"[pull] Writing updated JSON to {json_target}")
    design_to_json(design, json_target)


# -----------------------------
# 6. Generate: create schematic files
# -----------------------------


def _make_uuid() -> str:
    return str(uuid.uuid4())


def _escape_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _get_ref_prefix(refdes: str) -> str:
    match = re.match(r"^([A-Z]+)", refdes)
    return match.group(1) if match else "U"


def _next_annotated_ref(ref_counters: Dict[str, int], refdes: str) -> str:
    prefix = _get_ref_prefix(refdes)
    ref_counters[prefix] = ref_counters.get(prefix, 0) + 1
    return f"{prefix}{ref_counters[prefix]}"


def _build_annotation_plan(
    design: DesignData,
    root_uuid: str,
) -> Tuple[Dict[str, List[SymbolInstanceEntry]], Dict[str, List[SheetOccurrence]], Dict[str, Dict[str, List[SymbolInstanceEntry]]]]:
    ref_counters: Dict[str, int] = {}
    root_annotations: Dict[str, List[SymbolInstanceEntry]] = {}
    sheet_occurrences: Dict[str, List[SheetOccurrence]] = {}
    child_annotations: Dict[str, Dict[str, List[SymbolInstanceEntry]]] = {}

    root_path = f"/{root_uuid}"
    for comp in design.root_components:
        root_annotations[comp.refdes] = [
            SymbolInstanceEntry(
                path=root_path,
                reference=_next_annotated_ref(ref_counters, comp.refdes),
            )
        ]

    sheet_defs_by_id = {sheet.sheet_id: sheet for sheet in design.sheet_definitions}
    for idx, instance in enumerate(design.sheet_instances):
        if instance.sheet_id not in sheet_defs_by_id:
            raise ValueError(f"sheet_instances entry '{instance.instance_name}' references unknown sheet_id '{instance.sheet_id}'")

        sheet_uuid = _make_uuid()
        occurrence = SheetOccurrence(
            sheet_id=instance.sheet_id,
            instance_name=instance.instance_name,
            sheet_uuid=sheet_uuid,
            path=f"/{root_uuid}/{sheet_uuid}",
            page=str(idx + 2),
        )
        sheet_occurrences.setdefault(instance.sheet_id, []).append(occurrence)

        sheet_annotations = child_annotations.setdefault(instance.sheet_id, {})
        for comp in sheet_defs_by_id[instance.sheet_id].components:
            sheet_annotations.setdefault(comp.refdes, []).append(
                SymbolInstanceEntry(
                    path=occurrence.path,
                    reference=_next_annotated_ref(ref_counters, comp.refdes),
                )
            )

    return (root_annotations, sheet_occurrences, child_annotations)


def _snap_to_grid(value: float, grid: float = CONNECTION_GRID_MM) -> float:
    return round(value / grid) * grid


def _snap_up(value: float, grid: float = CONNECTION_GRID_MM) -> float:
    return math.ceil(value / grid) * grid


def _symbol_dimensions(pins: Sequence[Tuple[str, str]]) -> Tuple[float, float, int]:
    num_pins = len(pins)
    pins_per_side = (num_pins + 1) // 2
    height = max(pins_per_side * PIN_PITCH_MM + PIN_PITCH_MM, 7.62)

    left_pins = pins[:pins_per_side]
    right_pins = pins[pins_per_side:]
    left_name_width = max((_estimate_text_width(pin_name) for _pin_num, pin_name in left_pins), default=0.0)
    right_name_width = max((_estimate_text_width(pin_name) for _pin_num, pin_name in right_pins), default=0.0)

    # Keep a center gutter between left/right pin-name text so long names do not overlap.
    width = max(15.24, left_name_width + right_name_width + 10.16)
    width = _snap_up(width, PIN_PITCH_MM)
    height = _snap_up(height, PIN_PITCH_MM)
    return (width, height, pins_per_side)


def _generate_lib_symbol(symbol: ResolvedSymbol) -> str:
    lib_id = symbol.lib_id
    pins = _symbol_pin_pairs(symbol)
    ref_prefix = symbol.reference
    width, height, pins_per_side = _symbol_dimensions(pins)

    pin_defs = []
    left_pins = symbol.pins[:pins_per_side]
    right_pins = symbol.pins[pins_per_side:]

    for idx, pin in enumerate(left_pins):
        y = height / 2 - PIN_PITCH_MM - idx * PIN_PITCH_MM
        pin_defs.append(
            f'''
				(pin {pin.pin_type} line
					(at -{width/2 + PIN_PITCH_MM:.2f} {y:.2f} 0)
					(length {PIN_PITCH_MM:.2f})
					(name "{_escape_string(pin.name)}"
						(effects (font (size 1.27 1.27)))
					)
					(number "{_escape_string(pin.number)}"
						(effects (font (size 1.27 1.27)))
					)
				)'''
        )

    for idx, pin in enumerate(right_pins):
        y = height / 2 - PIN_PITCH_MM - idx * PIN_PITCH_MM
        pin_defs.append(
            f'''
				(pin {pin.pin_type} line
					(at {width/2 + PIN_PITCH_MM:.2f} {y:.2f} 180)
					(length {PIN_PITCH_MM:.2f})
					(name "{_escape_string(pin.name)}"
						(effects (font (size 1.27 1.27)))
					)
					(number "{_escape_string(pin.number)}"
						(effects (font (size 1.27 1.27)))
					)
				)'''
        )
    pins_str = "".join(pin_defs)
    symbol_leaf = _escape_string(lib_id.split(":")[-1])
    stroke_width = "0.254" if symbol.matched_from_library else "0.508"
    if symbol.matched_from_library:
        fill_block = """
					(fill
						(type background)
					)"""
        marker_text = ""
    else:
        fill_block = """
					(fill
						(type background)
					)"""
        marker_text = """
				(text "NOLIB"
					(at 0 0 0)
					(effects
						(font
							(size 1.27 1.27)
						)
					)
				)"""
    return f'''
		(symbol "{_escape_string(lib_id)}"
			(pin_names (offset 1.016))
			(exclude_from_sim no)
			(in_bom yes)
			(on_board yes)
			(property "Reference" "{_escape_string(ref_prefix)}"
				(at 0 {height/2 + 1.27:.2f} 0)
				(effects (font (size 1.27 1.27)))
			)
			(property "Value" "{symbol_leaf}"
				(at 0 {-height/2 - 1.27:.2f} 0)
				(effects (font (size 1.27 1.27)))
			)
			(property "Footprint" "{_escape_string(symbol.footprint)}"
				(at 0 0 0)
				(effects (font (size 1.27 1.27)) (hide yes))
			)
			(property "Datasheet" "{_escape_string(symbol.datasheet or '~')}"
				(at 0 0 0)
				(effects (font (size 1.27 1.27)) (hide yes))
			)
			(property "Description" "{_escape_string(symbol.description)}"
				(at 0 0 0)
				(effects (font (size 1.27 1.27)) (hide yes))
			)
			(symbol "{symbol_leaf}_0_1"
				(rectangle
					(start {-width/2:.2f} {height/2:.2f})
					(end {width/2:.2f} {-height/2:.2f})
					(stroke (width {stroke_width}) (type default))
{fill_block}
				)
{marker_text}
			)
			(symbol "{symbol_leaf}_1_1"{pins_str}
			)
		)'''


def _format_symbol_instances_block(project_name: str, entries: Sequence[SymbolInstanceEntry]) -> str:
    rendered_entries = "\n".join(
        [
            f'''				(path "{_escape_string(entry.path)}"
					(reference "{_escape_string(entry.reference)}")
					(unit 1)
				)'''
            for entry in entries
        ]
    )
    return f'''		(instances
			(project "{_escape_string(project_name)}"
{rendered_entries}
			)
		)'''


def _generate_symbol_instance(
    item: RenderComponent,
    x: float,
    y: float,
    project_name: str,
) -> str:
    symbol = item.symbol
    comp = item.component
    sym_uuid = _make_uuid()
    dnp_flag = "yes" if _component_is_dnp(comp) else "no"
    in_bom_flag = "yes" if comp.mpn.strip() and dnp_flag == "no" else "no"
    pins = _symbol_pin_pairs(symbol)
    pin_entries = "\n".join(
        [
            f'''		(pin "{_escape_string(pin_num)}"
			(uuid "{_make_uuid()}")
		)'''
            for pin_num, _ in pins
        ]
    )
    _width, height, _pins_per_side = _symbol_dimensions(pins)
    value_text = comp.mpn or symbol.name
    description_text = comp.description or symbol.description
    instance_entries = item.instance_entries or [SymbolInstanceEntry(path="/", reference=comp.refdes)]
    primary_reference = instance_entries[0].reference
    instances_block = _format_symbol_instances_block(project_name, instance_entries)
    return f'''
	(symbol
		(lib_id "{_escape_string(symbol.lib_id)}")
		(at {x:.2f} {y:.2f} 0)
		(unit 1)
		(exclude_from_sim no)
		(in_bom {in_bom_flag})
		(on_board yes)
		(dnp {dnp_flag})
		(fields_autoplaced yes)
		(uuid "{sym_uuid}")
		(property "Reference" "{_escape_string(primary_reference)}"
			(at {x:.2f} {y - height/2 - 3.81:.2f} 0)
			(effects (font (size 1.27 1.27)))
		)
		(property "Value" "{_escape_string(value_text)}"
			(at {x:.2f} {y - height/2 - 6.35:.2f} 0)
			(effects (font (size 1.27 1.27)))
		)
		(property "Footprint" "{_escape_string(symbol.footprint)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "Datasheet" "{_escape_string(symbol.datasheet or '~')}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "Description" "{_escape_string(description_text)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "JsonRefdes" "{_escape_string(comp.refdes)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "MPN" "{_escape_string(comp.mpn)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "LCSC" "{_escape_string(comp.lcsc)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "Notes" "{_escape_string(comp.notes)}"
			(at {x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
{pin_entries}
{instances_block}
	)'''


def _generate_label(label_type: str, net_name: str, x: float, y: float, pin_angle: float) -> str:
    label_uuid = _make_uuid()
    angle = label_angle_for_pin_angle(pin_angle)
    justify = label_justify_for_angle(angle)
    intersheet_x = intersheet_x_for_label(x, angle)
    if label_type == "local":
        return f'''
	(label "{_escape_string(net_name)}"
		(at {x:.2f} {y:.2f} {angle:g})
		(effects (font (size 1.27 1.27)) (justify {justify}))
		(uuid "{label_uuid}")
	)'''
    if label_type == "hierarchical":
        return f'''
	(hierarchical_label "{_escape_string(net_name)}"
		(shape bidirectional)
		(at {x:.2f} {y:.2f} {angle:g})
		(fields_autoplaced yes)
		(effects (font (size 1.27 1.27)) (justify {justify}))
		(uuid "{label_uuid}")
	)'''
    # global
    return f'''
	(global_label "{_escape_string(net_name)}"
		(shape bidirectional)
		(at {x:.2f} {y:.2f} {angle:g})
		(fields_autoplaced yes)
		(effects (font (size 1.27 1.27)) (justify {justify}))
		(uuid "{label_uuid}")
		(property "Intersheetrefs" "${{INTERSHEET_REFS}}"
			(at {intersheet_x:.2f} {y:.2f} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
	)'''


def _generate_wire_and_label(
    net_name: str,
    scope: str,
    pin: PinPosition,
    stub_len: float = 2.54,
) -> str:
    wire_uuid = _make_uuid()
    label_x, label_y = candidate_label_points_for_pin(pin.x, pin.y, pin.angle, stub_len)[0]
    return f'''
	(wire
		(pts (xy {pin.x:.2f} {pin.y:.2f}) (xy {label_x:.2f} {label_y:.2f}))
		(stroke (width 0) (type default))
		(uuid "{wire_uuid}")
	)
{_generate_label(scope, net_name, label_x, label_y, pin_angle=pin.angle)}'''


def _generate_no_connect(x: float, y: float) -> str:
    nc_uuid = _make_uuid()
    return f'''
	(no_connect
		(at {x:.2f} {y:.2f})
		(uuid "{nc_uuid}")
	)'''


def _estimate_text_width(text: str) -> float:
    # KiCad's default font is proportional, so use a conservative average width.
    return max(3.0, len(text) * 1.1)


def _component_layout_metrics(item: RenderComponent) -> Tuple[float, float, float, float]:
    comp = item.component
    pins = _symbol_pin_pairs(item.symbol)
    width, height, pins_per_side = _symbol_dimensions(pins)
    pin_anchor_dx = width / 2 + PIN_PITCH_MM
    stub_len = PIN_PITCH_MM

    left_pin_numbers = {pin.number for pin in item.symbol.pins[:pins_per_side]}
    right_pin_numbers = {pin.number for pin in item.symbol.pins[pins_per_side:]}
    left_conns = [conn for conn in comp.connections if conn.number in left_pin_numbers]
    right_conns = [conn for conn in comp.connections if conn.number in right_pin_numbers]
    left_label_width = max((_estimate_text_width(conn.net) for conn in left_conns if conn.net != NC_NET_NAME), default=0.0)
    right_label_width = max((_estimate_text_width(conn.net) for conn in right_conns if conn.net != NC_NET_NAME), default=0.0)

    left_extent = max(width / 2, pin_anchor_dx + stub_len + left_label_width)
    right_extent = max(width / 2, pin_anchor_dx + stub_len + right_label_width)

    # Reference/value are placed above the body; keep a matching bottom margin so rows stay visually open.
    top_extent = height / 2 + 9.0
    bottom_extent = height / 2 + 4.0
    return (left_extent, right_extent, top_extent, bottom_extent)


def _compute_component_centers(
    components: Sequence[RenderComponent],
    start_x: float,
    start_y: float,
    cols: int,
    col_gap: float = 12.0,
    row_gap: float = 14.0,
) -> List[Tuple[float, float]]:
    if not components:
        return []

    metrics = [_component_layout_metrics(comp) for comp in components]
    num_rows = (len(components) + cols - 1) // cols

    col_left = [0.0] * cols
    col_right = [0.0] * cols
    row_top = [0.0] * num_rows
    row_bottom = [0.0] * num_rows

    for idx, (left_extent, right_extent, top_extent, bottom_extent) in enumerate(metrics):
        col = idx % cols
        row = idx // cols
        col_left[col] = max(col_left[col], left_extent)
        col_right[col] = max(col_right[col], right_extent)
        row_top[row] = max(row_top[row], top_extent)
        row_bottom[row] = max(row_bottom[row], bottom_extent)

    x_centers = [_snap_to_grid(start_x)]
    for col in range(1, cols):
        prev = x_centers[-1]
        x_centers.append(_snap_up(prev + col_right[col - 1] + col_gap + col_left[col]))

    y_centers = [_snap_to_grid(start_y)]
    for row in range(1, num_rows):
        prev = y_centers[-1]
        y_centers.append(_snap_up(prev + row_bottom[row - 1] + row_gap + row_top[row]))

    centers: List[Tuple[float, float]] = []
    for idx in range(len(components)):
        col = idx % cols
        row = idx // cols
        centers.append((x_centers[col], y_centers[row]))
    return centers


def _component_layout_right_edge(
    components: Sequence[RenderComponent],
    centers: Sequence[Tuple[float, float]],
) -> float:
    if not components or not centers:
        return 0.0

    right_edge = 0.0
    for comp, (x, _y) in zip(components, centers):
        _left_extent, right_extent, _top_extent, _bottom_extent = _component_layout_metrics(comp)
        right_edge = max(right_edge, x + right_extent)
    return right_edge


def _symbol_pin_anchor_positions(
    symbol: ResolvedSymbol,
    x: float,
    y: float,
) -> Dict[str, PinPosition]:
    pins = _symbol_pin_pairs(symbol)
    width, height, pins_per_side = _symbol_dimensions(pins)
    pin_anchor_dx = width / 2 + PIN_PITCH_MM
    positions: Dict[str, PinPosition] = {}

    for idx, pin in enumerate(symbol.pins[:pins_per_side]):
        pin_y = y - (height / 2 - PIN_PITCH_MM - idx * PIN_PITCH_MM)
        positions[pin.number] = PinPosition(
            number=pin.number,
            name=pin.name,
            x=x - pin_anchor_dx,
            y=pin_y,
            angle=pin_angle_for_facing("left"),
        )

    for idx, pin in enumerate(symbol.pins[pins_per_side:]):
        pin_y = y - (height / 2 - PIN_PITCH_MM - idx * PIN_PITCH_MM)
        positions[pin.number] = PinPosition(
            number=pin.number,
            name=pin.name,
            x=x + pin_anchor_dx,
            y=pin_y,
            angle=pin_angle_for_facing("right"),
        )

    return positions


def _sheet_dimensions(pin_names: Sequence[str], instance_name: str, sheet_file: str) -> Tuple[float, float]:
    longest_pin = max((_estimate_text_width(name) for name in pin_names), default=0.0)
    title_width = _estimate_text_width(instance_name)
    file_width = _estimate_text_width(sheet_file)

    width = max(40.64, longest_pin + 14.0, title_width + 8.0, file_width + 8.0)
    height = max(30.48, 15.24 + max(0, len(pin_names) - 1) * PIN_PITCH_MM)
    width = _snap_up(width, PIN_PITCH_MM)
    height = _snap_up(height, PIN_PITCH_MM)
    return (width, height)


def _render_components_on_sheet(
    components: Sequence[RenderComponent],
    project_name: str,
    start_x: float = 50.0,
    start_y: float = 50.0,
) -> Tuple[str, str, str, Set[str]]:
    lib_symbols: List[str] = []
    symbol_instances: List[str] = []
    wires_and_labels: List[str] = []
    hierarchical_nets: Set[str] = set()

    cols = 4
    ordered_components = sorted(components, key=lambda c: c.component.refdes)
    centers = _compute_component_centers(ordered_components, start_x=start_x, start_y=start_y, cols=cols)
    emitted_lib_ids: Set[str] = set()

    for idx, item in enumerate(ordered_components):
        comp = item.component
        symbol = item.symbol
        x, y = centers[idx]

        if symbol.lib_id not in emitted_lib_ids:
            lib_symbols.append(_generate_lib_symbol(symbol))
            emitted_lib_ids.add(symbol.lib_id)
        symbol_instances.append(
            _generate_symbol_instance(
                item=item,
                x=x,
                y=y,
                project_name=project_name,
            )
        )

        pin_positions = _symbol_pin_anchor_positions(symbol, x, y)
        for conn in comp.connections:
            pin = pin_positions.get(conn.number)
            if pin is None:
                print(
                    f"[generate] WARNING: {comp.refdes} pin {conn.number} not present on symbol {symbol.lib_id}; skipping net {conn.net}"
                )
                continue
            if conn.net == NC_NET_NAME:
                wires_and_labels.append(_generate_no_connect(pin.x, pin.y))
                continue
            wires_and_labels.append(_generate_wire_and_label(conn.net, conn.scope, pin))
            if conn.scope == "hierarchical":
                hierarchical_nets.add(conn.net)

    return (
        "\n".join(lib_symbols),
        "\n".join(symbol_instances),
        "\n".join(wires_and_labels),
        hierarchical_nets,
    )


def _generate_sheet_pin(pin_name: str, x: float, y: float) -> str:
    return f'''
		(pin "{_escape_string(pin_name)}" bidirectional
			(at {x:.2f} {y:.2f} 180)
			(effects (font (size 1.27 1.27)) (justify left))
		)'''


def _generate_sheet_instances_block(sheet_paths: List[Tuple[str, str]]) -> str:
    # sheet_paths: [("/", "1"), ("/<uuid>", "2"), ...]
    entries = []
    for path, page in sheet_paths:
        entries.append(
            f'''		(path "{path}"
			(page "{page}")
		)'''
        )
    return "\n".join(entries)


def _attach_instance_entries(
    rendered_components: Sequence[RenderComponent],
    annotations_by_refdes: Dict[str, List[SymbolInstanceEntry]],
) -> List[RenderComponent]:
    attached: List[RenderComponent] = []
    for item in rendered_components:
        attached.append(
            RenderComponent(
                component=item.component,
                symbol=item.symbol,
                instance_entries=list(annotations_by_refdes.get(item.component.refdes, [])),
            )
        )
    return attached


def _generate_child_schematic(
    sheet_def: SheetDefinition,
    base_dir: Path,
    project_name: str,
    library_symbols: Sequence[ResolvedSymbol],
    annotations_by_refdes: Dict[str, List[SymbolInstanceEntry]],
    sheet_occurrences: Sequence[SheetOccurrence],
) -> Set[str]:
    child_uuid = _make_uuid()
    rendered_components = _attach_instance_entries(
        _resolve_render_components(sheet_def.components, library_symbols),
        annotations_by_refdes,
    )
    lib_symbols_str, symbols_str, wires_str, hierarchical_nets = _render_components_on_sheet(
        rendered_components,
        project_name=project_name,
    )
    sheet_paths = [(occ.path, occ.page) for occ in sheet_occurrences] or [("/", "1")]
    schematic = f'''(kicad_sch
	(version 20231120)
	(generator "json_schem_sync")
	(generator_version "2.0")
	(uuid "{child_uuid}")
	(paper "A3")
	(lib_symbols
{lib_symbols_str}
	)
{wires_str}
{symbols_str}
	(sheet_instances
{_generate_sheet_instances_block(sheet_paths)}
	)
)
'''
    child_path = base_dir / sheet_def.file
    child_path.parent.mkdir(parents=True, exist_ok=True)
    KicadSchematic.from_text(schematic).to_file(child_path)
    return hierarchical_nets


def generate_schematic_from_json(json_path: Path, sch_path: Path) -> None:
    design = design_from_json(json_path)
    project_name = sch_path.stem
    root_uuid = _make_uuid()
    root_annotations, sheet_occurrences, child_annotations = _build_annotation_plan(design, root_uuid)
    library_candidates = [
        json_path.parent / "libs.json",
        sch_path.parent / "libs.json",
        libs_json_path(),
    ]
    library_symbols: List[ResolvedSymbol] = []
    library_path: Optional[Path] = None
    for candidate in library_candidates:
        if candidate.exists():
            library_symbols = _load_library_symbols(candidate)
            library_path = candidate
            break
    if library_symbols and library_path is not None:
        print(f"[generate] Loaded {len(library_symbols)} symbols from {library_path}")

    # 1) Generate all child files first and capture their hierarchical interfaces.
    interface_by_sheet_id: Dict[str, Set[str]] = {}
    file_by_sheet_id: Dict[str, str] = {}
    for sheet_def in design.sheet_definitions:
        interface_by_sheet_id[sheet_def.sheet_id] = _generate_child_schematic(
            sheet_def,
            base_dir=sch_path.parent,
            project_name=project_name,
            library_symbols=library_symbols,
            annotations_by_refdes=child_annotations.get(sheet_def.sheet_id, {}),
            sheet_occurrences=sheet_occurrences.get(sheet_def.sheet_id, []),
        )
        file_by_sheet_id[sheet_def.sheet_id] = sheet_def.file

    # 2) Generate root symbols and nets.
    rendered_root_components = _attach_instance_entries(
        _resolve_render_components(design.root_components, library_symbols),
        root_annotations,
    )
    ordered_root_components = sorted(rendered_root_components, key=lambda c: c.component.refdes)
    root_centers = _compute_component_centers(
        ordered_root_components,
        start_x=50.0,
        start_y=50.0,
        cols=4,
    )
    lib_symbols_str, symbols_str, wires_str, _ = _render_components_on_sheet(
        rendered_root_components,
        project_name=project_name,
    )

    # 3) Place sheet instances in root and connect sheet pins.
    sheet_blocks: List[str] = []
    instance_labels: List[str] = []
    sheet_path_entries: List[Tuple[str, str]] = [("/", "1")]
    root_right_edge = _component_layout_right_edge(ordered_root_components, root_centers)
    sheet_x = _snap_up(max(220.0, root_right_edge + 25.0))
    sheet_y = _snap_to_grid(40.0)
    sheet_gap = _snap_up(18.0)

    for instance in design.sheet_instances:
        if instance.sheet_id not in interface_by_sheet_id:
            raise ValueError(
                f"sheet_instances entry '{instance.instance_name}' references unknown sheet_id '{instance.sheet_id}'"
            )
        matching_occurrences = sheet_occurrences.get(instance.sheet_id, [])
        instance_occurrence = next((occ for occ in matching_occurrences if occ.instance_name == instance.instance_name), None)
        if instance_occurrence is None:
            raise ValueError(
                f"Missing generated occurrence metadata for sheet instance '{instance.instance_name}'"
            )
        pin_names = sorted(interface_by_sheet_id[instance.sheet_id])
        pin_map = {p.name: p for p in instance.pin_map}
        sheet_file = file_by_sheet_id[instance.sheet_id]
        sheet_w, sheet_h = _sheet_dimensions(pin_names, instance.instance_name, sheet_file)
        y0 = sheet_y

        pin_lines = []
        for pin_idx, pin_name in enumerate(pin_names):
            pin_y = y0 + 7.62 + pin_idx * PIN_PITCH_MM
            pin_x = sheet_x
            pin_lines.append(_generate_sheet_pin(pin_name, pin_x, pin_y))

            mapped = pin_map.get(pin_name)
            if mapped is None:
                mapped = SheetPinMap(name=pin_name, net=pin_name, scope="global")
            instance_labels.append(
                _generate_wire_and_label(
                    mapped.net,
                    mapped.scope,
                    PinPosition(
                        number=pin_name,
                        name=pin_name,
                        x=pin_x,
                        y=pin_y,
                        angle=pin_angle_for_facing("left"),
                    ),
                )
            )

        sheet_blocks.append(
            f'''
	(sheet
		(at {sheet_x:.2f} {y0:.2f})
		(size {sheet_w:.2f} {sheet_h:.2f})
		(fields_autoplaced yes)
		(stroke (width 0) (type solid))
		(fill (color 0 0 0 0))
		(uuid "{instance_occurrence.sheet_uuid}")
		(property "Sheet name" "{_escape_string(instance.instance_name)}"
			(at {sheet_x + 1:.2f} {y0 + 1.5:.2f} 0)
			(effects (font (size 1.27 1.27)))
		)
		(property "Sheet file" "{_escape_string(sheet_file)}"
			(at {sheet_x + 1:.2f} {y0 + 5.0:.2f} 0)
			(effects (font (size 1.27 1.27)))
		)
{"".join(pin_lines)}
	)'''
        )
        sheet_path_entries.append((instance_occurrence.path, instance_occurrence.page))
        sheet_y = y0 + sheet_h + sheet_gap

    schematic = f'''(kicad_sch
	(version 20231120)
	(generator "json_schem_sync")
	(generator_version "2.0")
	(uuid "{root_uuid}")
	(paper "A3")
	(lib_symbols
{lib_symbols_str}
	)
{wires_str}
{"".join(instance_labels)}
{"".join(sheet_blocks)}
{symbols_str}
	(sheet_instances
{_generate_sheet_instances_block(sheet_path_entries)}
	)
)
'''
    KicadSchematic.from_text(schematic).to_file(sch_path)
    print(f"[generate] Created root schematic: {sch_path}")
    print(f"[generate] Root components: {len(design.root_components)}")
    print(f"[generate] Child definitions: {len(design.sheet_definitions)}")
    print(f"[generate] Sheet instances: {len(design.sheet_instances)}")


# -----------------------------
# 7. List Library Components
# -----------------------------


def _get_default_symbol_dir() -> Path:
    """Get the default KiCad symbol library directory based on OS."""
    system = platform.system()
    if system == "Darwin":  # macOS
        # Try KiCad 9 first, then 8
        paths = [
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
            Path("/Applications/Kicad/kicad.app/Contents/SharedSupport/symbols"),
        ]
    elif system == "Windows":
        paths = [
            Path("C:/Program Files/KiCad/share/kicad/symbols"),
            Path("C:/Program Files/KiCad/9.0/share/kicad/symbols"),
            Path("C:/Program Files/KiCad/8.0/share/kicad/symbols"),
        ]
    else:  # Linux
        paths = [
            Path("/usr/share/kicad/symbols"),
            Path("/usr/local/share/kicad/symbols"),
        ]
    
    for p in paths:
        if p.exists():
            return p
    
    # Return first path even if it doesn't exist (let caller handle error)
    return paths[0]


def _parse_symbol_property(prop_sexp: List[Any]) -> Tuple[str, str]:
    """
    Parse a property S-expression like:
    (property "Description" "Some description" ...)
    
    Returns (property_name, property_value)
    """
    if len(prop_sexp) < 3:
        return ("", "")
    
    # prop_sexp[0] is Symbol('property')
    # prop_sexp[1] is the property name
    # prop_sexp[2] is the property value
    name = prop_sexp[1] if isinstance(prop_sexp[1], str) else str(prop_sexp[1])
    value = prop_sexp[2] if isinstance(prop_sexp[2], str) else str(prop_sexp[2])
    return (name, value)


def _parse_pin(pin_sexp: List[Any]) -> Dict[str, str]:
    """
    Parse a pin S-expression like:
    (pin input line (at x y angle) (length l) (name "NAME" ...) (number "1" ...))
    
    Returns dict with pin info.
    """
    pin_info = {"type": "", "name": "", "number": ""}
    
    if len(pin_sexp) < 2:
        return pin_info
    
    # pin_sexp[1] is the pin type (input, output, etc.)
    if hasattr(pin_sexp[1], 'value'):
        pin_info["type"] = pin_sexp[1].value()
    else:
        pin_info["type"] = str(pin_sexp[1])
    
    # Look for name and number in nested elements
    for item in pin_sexp[2:]:
        if isinstance(item, list) and len(item) >= 2:
            if hasattr(item[0], 'value'):
                key = item[0].value()
            else:
                key = str(item[0])
            
            if key == "name" and len(item) >= 2:
                pin_info["name"] = item[1] if isinstance(item[1], str) else str(item[1])
            elif key == "number" and len(item) >= 2:
                pin_info["number"] = item[1] if isinstance(item[1], str) else str(item[1])
    
    return pin_info


def _extract_symbol_from_sexp(sym_sexp: List[Any], library_name: str) -> Optional[Dict[str, Any]]:
    """
    Extract symbol information from a parsed S-expression.
    
    Returns a dict with symbol info or None if invalid.
    """
    if len(sym_sexp) < 2:
        return None
    
    # sym_sexp[1] is the symbol name
    symbol_name = sym_sexp[1] if isinstance(sym_sexp[1], str) else str(sym_sexp[1])
    
    # Skip sub-unit symbols (e.g., "SymbolName_1_1")
    if re.match(r'.+_\d+_\d+$', symbol_name):
        return None
    
    properties = {}
    pins = []
    
    for item in sym_sexp[2:]:
        if not isinstance(item, list) or len(item) < 1:
            continue
        
        # Get the element type
        if hasattr(item[0], 'value'):
            elem_type = item[0].value()
        else:
            elem_type = str(item[0])
        
        if elem_type == "property":
            name, value = _parse_symbol_property(item)
            if name:
                properties[name] = value
        elif elem_type == "pin":
            pin_info = _parse_pin(item)
            if pin_info["number"]:
                pins.append(pin_info)
        elif elem_type == "symbol":
            # This is a sub-unit, look for pins inside
            for sub_item in item[2:]:
                if isinstance(sub_item, list) and len(sub_item) >= 1:
                    if hasattr(sub_item[0], 'value'):
                        sub_type = sub_item[0].value()
                    else:
                        sub_type = str(sub_item[0])
                    if sub_type == "pin":
                        pin_info = _parse_pin(sub_item)
                        if pin_info["number"]:
                            pins.append(pin_info)
    
    return {
        "library": library_name,
        "name": symbol_name,
        "lib_id": f"{library_name}:{symbol_name}",
        "reference": properties.get("Reference", ""),
        "value": properties.get("Value", ""),
        "footprint": properties.get("Footprint", ""),
        "datasheet": properties.get("Datasheet", ""),
        "description": properties.get("Description", ""),
        "keywords": properties.get("ki_keywords", ""),
        "pins": pins,
    }


def parse_kicad_symbol_library(lib_path: Path) -> List[Dict[str, Any]]:
    """
    Parse a .kicad_sym file and return list of symbol dictionaries.
    """
    symbols = []
    library_name = lib_path.stem
    
    try:
        content = lib_path.read_text(encoding="utf-8")
        parsed = sexpdata.loads(content)
    except Exception as e:
        print(f"[list-libs] WARNING: Could not parse {lib_path}: {e}")
        return symbols
    
    # parsed is a list like [Symbol('kicad_symbol_lib'), ...]
    if not isinstance(parsed, list):
        return symbols
    
    for item in parsed[1:]:  # Skip the header symbol
        if not isinstance(item, list) or len(item) < 2:
            continue
        
        # Check if this is a symbol definition
        if hasattr(item[0], 'value'):
            elem_type = item[0].value()
        else:
            elem_type = str(item[0])
        
        if elem_type == "symbol":
            sym_info = _extract_symbol_from_sexp(item, library_name)
            if sym_info:
                symbols.append(sym_info)
    
    return symbols


def list_library_components(
    lib_dir: Path,
    filter_text: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> None:
    """
    List all components from KiCad symbol libraries.
    
    Args:
        lib_dir: Directory containing .kicad_sym files
        filter_text: Only include components with this text in description (e.g. "(Good)")
        output_path: Path to write JSON output (prints to stdout if None)
    """
    if not lib_dir.exists():
        print(f"[list-libs] ERROR: Library directory not found: {lib_dir}")
        print(f"           Please specify a valid path with --lib-dir")
        return
    
    # Find all .kicad_sym files
    sym_files = sorted(lib_dir.glob("*.kicad_sym"))
    if not sym_files:
        print(f"[list-libs] ERROR: No .kicad_sym files found in {lib_dir}")
        return
    
    print(f"[list-libs] Scanning {len(sym_files)} library files in {lib_dir}")
    
    all_symbols = []
    for sym_file in sym_files:
        symbols = parse_kicad_symbol_library(sym_file)
        all_symbols.extend(symbols)
    
    print(f"[list-libs] Found {len(all_symbols)} total symbols")
    
    # Apply filter if specified
    if filter_text:
        filtered = [
            s for s in all_symbols
            if filter_text.lower() in s.get("description", "").lower()
        ]
        print(f"[list-libs] After filtering for '{filter_text}': {len(filtered)} symbols")
        all_symbols = filtered
    
    # Convert to JSON-friendly format
    output = []
    for sym in all_symbols:
        output.append({
            "lib_id": sym["lib_id"],
            "library": sym["library"],
            "name": sym["name"],
            "reference": sym["reference"],
            "description": sym["description"],
            "footprint": sym["footprint"],
            "datasheet": sym["datasheet"],
            "keywords": sym["keywords"],
            "pins": [
                {"number": p["number"], "name": p["name"], "type": p["type"]}
                for p in sym["pins"]
            ],
        })
    
    json_output = json.dumps(output, indent=2)
    
    if output_path:
        output_path.write_text(json_output, encoding="utf-8")
        print(f"[list-libs] Wrote {len(output)} components to {output_path}")
    else:
        print(json_output)


# -----------------------------
# 8. CLI
# -----------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync simple JSON netlist ↔ KiCad .kicad_sch using kicad-skip.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_generate = sub.add_parser("generate", help="Generate new schematic from JSON (creates symbols from scratch)")
    p_generate.add_argument("--json", type=Path, required=True, help="Input JSON file")
    p_generate.add_argument("--sch", type=Path, required=True, help="Output schematic (.kicad_sch)")

    p_push = sub.add_parser("push", help="Push JSON → existing schematic (updates properties and adds labels)")
    p_push.add_argument("--json", type=Path, required=True, help="Input JSON file")
    p_push.add_argument("--sch", type=Path, required=True, help="Input schematic (.kicad_sch)")
    p_push.add_argument(
        "--out-sch",
        type=Path,
        help="Output schematic path (default: overwrite --sch)",
    )

    p_pull = sub.add_parser("pull", help="Pull schematic → JSON")
    p_pull.add_argument("--json", type=Path, required=True, help="Input JSON file")
    p_pull.add_argument("--sch", type=Path, required=True, help="Input schematic (.kicad_sch)")
    p_pull.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON (default: overwrite --json)",
    )
    p_pull.add_argument(
        "--radius",
        type=float,
        default=5.0,
        help="Search radius (KiCad units) when looking for labels near pins",
    )

    p_list = sub.add_parser("list-libs", help="List components from KiCad symbol libraries")
    p_list.add_argument(
        "--lib-dir",
        type=Path,
        default=None,
        help="Directory containing .kicad_sym files (default: auto-detect KiCad install)",
    )
    p_list.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter components by text in description (e.g., '(Good)')",
    )
    p_list.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON file path (default: print to stdout)",
    )

    args = parser.parse_args(argv)

    if args.cmd == "generate":
        generate_schematic_from_json(args.json, args.sch)
    elif args.cmd == "push":
        push_json_to_schematic(args.json, args.sch, args.out_sch)
    elif args.cmd == "pull":
        pull_schematic_to_json(args.json, args.sch, args.out_json, args.radius)
    elif args.cmd == "list-libs":
        lib_dir = args.lib_dir if args.lib_dir else _get_default_symbol_dir()
        list_library_components(lib_dir, args.filter, args.output)


if __name__ == "__main__":
    main()
