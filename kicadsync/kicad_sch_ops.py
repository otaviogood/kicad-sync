from __future__ import annotations

import hashlib
import math
import uuid
import copy
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .kicad_sch_model import KicadSchematic, child_lists, find_child, get_property_value
from .kicad_sch_sexp import Atom, SExp, as_text, atom, list_head, qstr


@dataclass
class PinPosition:
    number: str
    name: str
    x: float
    y: float
    angle: float = 180.0


@dataclass
class NearbyLabel:
    scope: str
    net: str
    x: float
    y: float
    distance: float


@dataclass(frozen=True)
class LabelGeometry:
    wire_end_x: float
    wire_end_y: float
    label_angle: float
    justify: str
    intersheet_x: float


def scope_to_label_head(scope: str) -> str:
    if scope == "local":
        return "label"
    if scope == "hierarchical":
        return "hierarchical_label"
    return "global_label"


def label_head_to_scope(head: str) -> str:
    if head == "label":
        return "local"
    if head == "hierarchical_label":
        return "hierarchical"
    return "global"


def pin_angle_for_facing(facing: str) -> float:
    return 0.0 if facing == "left" else 180.0


def label_angle_for_pin_angle(pin_angle: float) -> float:
    return 180.0 if _normalized_angle(pin_angle) == 0 else 0.0


def label_justify_for_angle(label_angle: float) -> str:
    return "right" if _normalized_angle(label_angle) == 180 else "left"


def intersheet_x_for_label(label_x: float, label_angle: float) -> float:
    return label_x - 1.0 if _normalized_angle(label_angle) == 180 else label_x + 1.0


def candidate_label_points_for_pin(pin_x: float, pin_y: float, pin_angle: float, stub_len: float) -> List[Tuple[float, float]]:
    preferred_dx = stub_len if _normalized_angle(pin_angle) == 180 else -stub_len
    preferred = (pin_x + preferred_dx, pin_y)
    alternate = (pin_x - preferred_dx, pin_y)
    return [preferred, alternate]


def label_geometry_for_pin(pin_x: float, pin_y: float, pin_angle: float, stub_len: float) -> LabelGeometry:
    wire_end_x, wire_end_y = candidate_label_points_for_pin(pin_x, pin_y, pin_angle, stub_len)[0]
    label_angle = label_angle_for_pin_angle(pin_angle)
    return LabelGeometry(
        wire_end_x=wire_end_x,
        wire_end_y=wire_end_y,
        label_angle=label_angle,
        justify=label_justify_for_angle(label_angle),
        intersheet_x=intersheet_x_for_label(wire_end_x, label_angle),
    )


OWNED_UUID_PREFIX = "c0de9ace"
_PIN_ARTIFACT_HEADS = ("label", "hierarchical_label", "global_label", "wire", "no_connect")


def owned_uuid_fragment(owner: str) -> str:
    """UUID prefix shared by every artifact kicadsync placed for `owner` (a
    refdes, or a sheet-instance owner key). Same c0de9ace namespace as the PCB
    annotations, plus two groups identifying the owning symbol."""
    digest = hashlib.sha1(f"owner:{owner}".encode("utf-8")).hexdigest()
    return f"{OWNED_UUID_PREFIX}-{digest[0:4]}-4{digest[4:7]}-"


def owned_artifact_uuid(namespace: str, owner: str, kind: str, pin_number: str) -> str:
    """Stable UUID for a tool-placed pin artifact (label / stub wire /
    no_connect): deterministic so idempotent pushes don't churn the file, and
    owner-tagged so a later push can sweep a symbol's artifacts no matter
    where geometry drift left them."""
    tail = hashlib.sha1(f"{namespace}|{owner}|{kind}|{pin_number}".encode("utf-8")).hexdigest()
    return f"{owned_uuid_fragment(owner)}8{tail[0:3]}-{tail[3:15]}"


def node_uuid(node: List[SExp]) -> str:
    uuid_node = find_child(node, "uuid")
    if uuid_node is None or len(uuid_node) < 2 or isinstance(uuid_node[1], list):
        return ""
    return as_text(uuid_node[1])


def set_node_uuid(node: List[SExp], value: str) -> None:
    uuid_node = find_child(node, "uuid")
    if uuid_node is not None and len(uuid_node) >= 2:
        uuid_node[1] = qstr(value)
    else:
        node.append([atom("uuid"), qstr(value)])


def claim_pin_wire_and_label(
    schem: KicadSchematic,
    pin: PinPosition,
    net_name: str,
    scope: str,
    namespace: str,
    owner: str,
    claimed_ids: Set[int],
    stub_len: float = 2.54,
) -> None:
    """Stamp ownership onto the stub wire + label serving `pin`, whether this
    push just placed them or an older kicadsync left them with random uuids
    (adoption). Claimed nodes survive remove_unclaimed_owned_artifacts."""
    label_head = scope_to_label_head(scope)
    candidates = candidate_label_points_for_pin(pin.x, pin.y, pin.angle, stub_len)
    target = None
    for cx, cy in candidates:
        if _find_label(schem, label_head, net_name, cx, cy) is not None:
            target = (cx, cy)
            break
    if target is None:
        for cx, cy in candidates:
            if _find_wire(schem, pin.x, pin.y, cx, cy) is not None:
                target = (cx, cy)
                break
    if target is None:
        return
    label = _find_label(schem, label_head, net_name, target[0], target[1])
    if label is not None:
        set_node_uuid(label, owned_artifact_uuid(namespace, owner, "label", pin.number))
        claimed_ids.add(id(label))
    wire = _find_wire(schem, pin.x, pin.y, target[0], target[1])
    if wire is not None:
        set_node_uuid(wire, owned_artifact_uuid(namespace, owner, "wire", pin.number))
        claimed_ids.add(id(wire))


def claim_pin_no_connect(
    schem: KicadSchematic,
    pin: PinPosition,
    namespace: str,
    owner: str,
    claimed_ids: Set[int],
) -> None:
    for marker in schem.top_level_nodes("no_connect"):
        at = _find_at(marker)
        if at is not None and _close(at[0], pin.x) and _close(at[1], pin.y):
            set_node_uuid(marker, owned_artifact_uuid(namespace, owner, "no_connect", pin.number))
            claimed_ids.add(id(marker))
            return


def remove_unclaimed_owned_artifacts(
    schem: KicadSchematic,
    owner: str,
    claimed_ids: Set[int],
) -> List[Tuple[str, str]]:
    """Delete every artifact owned by `owner` that the current push didn't
    claim. Ownership lives in the uuid, not the position, so labels/wires
    stranded at old pin positions by a symbol re-render are swept wherever
    they drifted. Returns the removed (head, label_text) pairs."""
    fragment = owned_uuid_fragment(owner)
    removed: List[Tuple[str, str]] = []
    for head in _PIN_ARTIFACT_HEADS:
        for node in list(schem.top_level_nodes(head)):
            if id(node) in claimed_ids:
                continue
            if not node_uuid(node).startswith(fragment):
                continue
            schem.remove_top_level(node)
            removed.append((head, _node_label_value(node)))
    return removed


def find_symbol_by_reference(schem: KicadSchematic, refdes: str) -> Optional[List[SExp]]:
    for sym in schem.top_level_nodes("symbol"):
        if get_property_value(sym, "Reference") == refdes:
            return sym
    return None


def find_symbol_by_property(schem: KicadSchematic, name: str, value: str) -> Optional[List[SExp]]:
    for sym in schem.top_level_nodes("symbol"):
        if get_property_value(sym, name) == value:
            return sym
    return None


def set_symbol_property(symbol: List[SExp], name: str, value: str) -> None:
    for prop in child_lists(symbol, "property"):
        if len(prop) >= 3 and as_text(prop[1]) == name:
            prop[2] = qstr(value)
            return

    new_prop = [
        atom("property"),
        qstr(name),
        qstr(value),
        [atom("at"), atom("0"), atom("0"), atom("0")],
        [atom("effects"), [atom("font"), [atom("size"), atom("1.27"), atom("1.27")]], [atom("hide"), atom("yes")]],
    ]
    symbol.append(new_prop)


def set_symbol_lib_id(symbol: List[SExp], lib_id: str) -> None:
    existing = find_child(symbol, "lib_id")
    if existing is not None and len(existing) >= 2:
        existing[1] = qstr(lib_id)
        return
    symbol.insert(1, [atom("lib_id"), qstr(lib_id)])


def get_symbol_property(symbol: List[SExp], name: str) -> str:
    value = get_property_value(symbol, name)
    return value if value is not None else ""


def find_symbol_pin(symbol: List[SExp], pin_number: str, pin_name: str, schem: KicadSchematic) -> PinPosition:
    pin_map = _pin_positions_for_symbol(symbol, schem)
    if pin_number in pin_map:
        return pin_map[pin_number]
    for pin in pin_map.values():
        if pin.name == pin_name:
            return pin
    raise KeyError(
        f"Could not find pin number='{pin_number}' name='{pin_name}' on symbol {get_symbol_property(symbol, 'Reference')}"
    )


def infer_label_near_pin(
    schem: KicadSchematic,
    pin: PinPosition,
    search_radius: float,
) -> Optional[NearbyLabel]:
    search_order = [("local", "label"), ("hierarchical", "hierarchical_label"), ("global", "global_label")]
    best_by_scope: Dict[str, NearbyLabel] = {}

    for scope, head in search_order:
        for label in schem.top_level_nodes(head):
            at = _find_at(label)
            if at is None:
                continue
            lx, ly = at[0], at[1]
            dist = math.dist((pin.x, pin.y), (lx, ly))
            if dist <= search_radius:
                prev = best_by_scope.get(scope)
                if prev is None or dist < prev.distance:
                    best_by_scope[scope] = NearbyLabel(scope=scope, net=_node_label_value(label), x=lx, y=ly, distance=dist)

    for scope, _ in search_order:
        if scope in best_by_scope:
            return best_by_scope[scope]
    return None


def add_wire_and_label_for_pin(
    schem: KicadSchematic,
    pin: PinPosition,
    net_name: str,
    scope: str,
    stub_len: float = 2.54,
) -> bool:
    x1 = pin.x
    y1 = pin.y
    label_head = scope_to_label_head(scope)
    candidate_points = candidate_label_points_for_pin(pin.x, pin.y, pin.angle, stub_len)
    geometry = label_geometry_for_pin(pin.x, pin.y, pin.angle, stub_len)
    x2 = geometry.wire_end_x
    y2 = geometry.wire_end_y

    for candidate_x, candidate_y in candidate_points:
        wire_exists = _has_wire(schem, x1, y1, candidate_x, candidate_y)
        exact_label = _find_label(schem, label_head, net_name, candidate_x, candidate_y)
        label_at_point = _find_any_label_at_position(schem, candidate_x, candidate_y)
        if wire_exists and exact_label is not None:
            removed = _remove_conflicting_labels(schem, label_head, net_name, candidate_x, candidate_y)
            if _remove_other_labels_at_position(schem, candidate_x, candidate_y, keep=exact_label):
                removed = True
            return removed
        if wire_exists and label_at_point is not None:
            changed = False
            if list_head(label_at_point) != label_head or _node_label_value(label_at_point) != net_name:
                _replace_label_node(label_at_point, _new_label(label_head, net_name, candidate_x, candidate_y, geometry.label_angle))
                changed = True
            if _remove_other_labels_at_position(schem, candidate_x, candidate_y, keep=label_at_point):
                changed = True
            return changed
        if wire_exists:
            schem.append_top_level(_new_label(label_head, net_name, candidate_x, candidate_y, geometry.label_angle))
            return True
        nearby_same_net = _find_any_label_for_net(schem, net_name, candidate_x, candidate_y)
        if nearby_same_net is not None:
            changed = False
            if not wire_exists:
                schem.append_top_level(_new_wire(x1, y1, candidate_x, candidate_y))
                changed = True
            if list_head(nearby_same_net) != label_head:
                _replace_label_node(nearby_same_net, _new_label(label_head, net_name, candidate_x, candidate_y, geometry.label_angle))
                changed = True
            if _remove_conflicting_labels(schem, label_head, net_name, candidate_x, candidate_y):
                changed = True
            if _remove_other_labels_at_position(schem, candidate_x, candidate_y, keep=nearby_same_net):
                changed = True
            return changed

    changed = False
    if not _has_wire(schem, x1, y1, x2, y2):
        schem.append_top_level(_new_wire(x1, y1, x2, y2))
        changed = True
    exact_label = _find_label(schem, label_head, net_name, x2, y2)
    label_at_point = _find_any_label_at_position(schem, x2, y2)
    if exact_label is None and label_at_point is None:
        schem.append_top_level(_new_label(label_head, net_name, x2, y2, geometry.label_angle))
        changed = True
    elif exact_label is None and label_at_point is not None:
        _replace_label_node(label_at_point, _new_label(label_head, net_name, x2, y2, geometry.label_angle))
        changed = True
    if _remove_conflicting_labels(schem, label_head, net_name, x2, y2):
        changed = True
    keep_label = _find_label(schem, label_head, net_name, x2, y2)
    if _remove_other_labels_at_position(schem, x2, y2, keep=keep_label):
        changed = True
    return changed


def add_no_connect_for_pin(
    schem: KicadSchematic,
    pin: PinPosition,
) -> bool:
    if _has_no_connect(schem, pin.x, pin.y):
        return False
    schem.append_top_level(_new_no_connect(pin.x, pin.y))
    return True


def remove_stale_labels(schem: KicadSchematic, live_net_names: Set[str]) -> List[Tuple[str, str]]:
    """Remove labels whose net name no longer exists anywhere in the design.

    The design JSON is the source of truth for net *names*; geometry (wires,
    label positions) belongs to the schematic. A label carrying a name absent
    from ``live_net_names`` is dead regardless of where the user moved it, so
    it is safe to delete without touching manual wiring. Returns the removed
    (label_kind, net_name) pairs.
    """
    removed: List[Tuple[str, str]] = []
    for head in ("label", "hierarchical_label", "global_label"):
        for label in list(schem.top_level_nodes(head)):
            name = _node_label_value(label)
            if name and name not in live_net_names:
                schem.remove_top_level(label)
                removed.append((head, name))
    return removed


def remove_orphan_stub_wires(
    schem: KicadSchematic, stub_len: float = 2.54
) -> List[Tuple[float, float, float, float, str]]:
    """Remove stub wires whose removal provably cannot change the netlist.

    Two cases, both restricted to unowned wires of exactly one stub length
    (the signature of a stranded pre-ownership pin stub):

    - "connected to nothing": touches no pin, label, no-connect, junction, or
      other wire — electrically inert.
    - "redundant with an overlapping wire": every connection point it touches
      (pins/labels/junctions on it, wire-touch points) lies on one single
      other wire, so everything it joins stays joined through that wire. This
      is what a stranded stub looks like after its symbol moved a small step
      and the replacement stub overlaps it.

    Returns the removed segments as (x1, y1, x2, y2, reason).
    """
    pin_points: Set[Tuple[float, float]] = set()
    for sym in schem.top_level_nodes("symbol"):
        for pin in _pin_positions_for_symbol(sym, schem).values():
            pin_points.add((round(pin.x, 2), round(pin.y, 2)))

    anchor_points: Set[Tuple[float, float]] = set(pin_points)
    for head in ("label", "hierarchical_label", "global_label", "no_connect", "junction"):
        for node in schem.top_level_nodes(head):
            at = _find_at(node)
            if at is None:
                continue
            anchor_points.add((round(at[0], 2), round(at[1], 2)))

    def wire_segment(wire: List[SExp]) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        pts = find_child(wire, "pts")
        if pts is None:
            return None
        xy = child_lists(pts, "xy")
        if len(xy) != 2 or len(xy[0]) < 3 or len(xy[1]) < 3:
            return None
        ax, ay = _parse_float(xy[0][1]), _parse_float(xy[0][2])
        bx, by = _parse_float(xy[1][1]), _parse_float(xy[1][2])
        if None in (ax, ay, bx, by):
            return None
        return ((ax, ay), (bx, by))

    def point_on_segment(px: float, py: float, a: Tuple[float, float], b: Tuple[float, float]) -> bool:
        if not (min(a[0], b[0]) - 0.02 <= px <= max(a[0], b[0]) + 0.02):
            return False
        if not (min(a[1], b[1]) - 0.02 <= py <= max(a[1], b[1]) + 0.02):
            return False
        cross = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
        length = math.dist(a, b)
        if length < 0.02:
            return math.dist((px, py), a) <= 0.02
        return abs(cross) / length <= 0.02

    wires = [(w, wire_segment(w)) for w in schem.top_level_nodes("wire")]
    dropped: Set[int] = set()
    removed: List[Tuple[float, float, float, float, str]] = []
    for wire, seg in wires:
        if seg is None or node_uuid(wire).startswith(OWNED_UUID_PREFIX + "-"):
            continue
        a, b = seg
        if abs(math.dist(a, b) - stub_len) > 0.01:
            continue
        if not (_close(a[0], b[0]) or _close(a[1], b[1])):
            continue

        # Every connection point this wire provides: pins/labels/junctions on
        # it, plus each point where it and another wire touch.
        contacts: List[Tuple[float, float]] = []
        for px, py in anchor_points:
            if point_on_segment(px, py, a, b):
                contacts.append((px, py))
        others = [(ow, oseg) for ow, oseg in wires if ow is not wire and id(ow) not in dropped and oseg is not None]
        for _, oseg in others:
            for p in oseg:
                if point_on_segment(p[0], p[1], a, b):
                    contacts.append(p)
            for p in (a, b):
                if point_on_segment(p[0], p[1], oseg[0], oseg[1]):
                    contacts.append(p)

        if not contacts:
            reason = "connected to nothing"
        else:
            covered_by_one = any(
                all(point_on_segment(cx, cy, oseg[0], oseg[1]) for cx, cy in contacts) for _, oseg in others
            )
            if not covered_by_one:
                continue
            reason = "redundant with an overlapping wire"
        schem.remove_top_level(wire)
        dropped.add(id(wire))
        removed.append((a[0], a[1], b[0], b[1], reason))
    return removed


def sync_schematic_normalized(schem: KicadSchematic) -> None:
    # Keep a stable ordering where top-level sheet_instances stays near the end if present.
    # Unknown nodes remain in place unless we append new wire/label entries.
    pass


def _pin_positions_for_symbol(symbol: List[SExp], schem: KicadSchematic) -> Dict[str, PinPosition]:
    lib_id = _symbol_lib_id(symbol)
    if not lib_id:
        return {}

    symbol_at = _find_at(symbol)
    if symbol_at is None:
        return {}
    sx, sy, srot = symbol_at

    pin_defs = _find_lib_symbol_pins(schem, lib_id)
    out: Dict[str, PinPosition] = {}
    for pin_num, pin_name, px, py, pangle in pin_defs:
        rx, ry = _rotate(px, -py, srot)
        out[pin_num] = PinPosition(
            number=pin_num,
            name=pin_name,
            x=sx + rx,
            y=sy + ry,
            angle=(pangle + srot) % 360,
        )
    return out


def _symbol_lib_id(symbol: List[SExp]) -> str:
    lib_id = find_child(symbol, "lib_id")
    if not lib_id or len(lib_id) < 2:
        return ""
    return as_text(lib_id[1])


def _find_lib_symbol_pins(schem: KicadSchematic, lib_id: str) -> List[Tuple[str, str, float, float, float]]:
    lib_symbols = schem.find_first("lib_symbols")
    if lib_symbols is None:
        return []

    target = None
    for entry in child_lists(lib_symbols, "symbol"):
        if len(entry) >= 2 and as_text(entry[1]) == lib_id:
            target = entry
            break
    if target is None:
        return []

    pins: List[Tuple[str, str, float, float, float]] = []
    _collect_pins_recursive(target, pins)
    return pins


def _collect_pins_recursive(node: List[SExp], out: List[Tuple[str, str, float, float, float]]) -> None:
    for child in child_lists(node):
        head = list_head(child)
        if head == "pin":
            pin_num = ""
            pin_name = ""
            at = _find_at(child)
            if at is None:
                continue
            for sub in child_lists(child):
                if list_head(sub) == "name" and len(sub) >= 2:
                    pin_name = as_text(sub[1])
                elif list_head(sub) == "number" and len(sub) >= 2:
                    pin_num = as_text(sub[1])
            if pin_num:
                out.append((pin_num, pin_name, at[0], at[1], at[2]))
        elif head == "symbol":
            _collect_pins_recursive(child, out)


def _find_at(node: List[SExp]) -> Optional[Tuple[float, float, float]]:
    at = find_child(node, "at")
    if at is None or len(at) < 3:
        return None
    x = _parse_float(at[1])
    y = _parse_float(at[2])
    angle = _parse_float(at[3]) if len(at) > 3 else 0.0
    if x is None or y is None:
        return None
    return (x, y, angle)


def _parse_float(node: SExp) -> Optional[float]:
    if isinstance(node, list):
        return None
    try:
        return float(as_text(node))
    except ValueError:
        return None


def _rotate(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
    angle = int(round(angle_deg)) % 360
    if angle == 0:
        return (x, y)
    if angle == 90:
        return (-y, x)
    if angle == 180:
        return (-x, -y)
    if angle == 270:
        return (y, -x)
    # Fallback for odd angles.
    rad = math.radians(angle_deg)
    return (x * math.cos(rad) - y * math.sin(rad), x * math.sin(rad) + y * math.cos(rad))


def _node_label_value(label_node: List[SExp]) -> str:
    if len(label_node) < 2:
        return ""
    if isinstance(label_node[1], list):
        return ""
    return as_text(label_node[1])


def _find_wire(schem: KicadSchematic, x1: float, y1: float, x2: float, y2: float) -> Optional[List[SExp]]:
    for wire in schem.top_level_nodes("wire"):
        pts = find_child(wire, "pts")
        if pts is None:
            continue
        xy = child_lists(pts, "xy")
        if len(xy) != 2 or len(xy[0]) < 3 or len(xy[1]) < 3:
            continue
        a = (_parse_float(xy[0][1]), _parse_float(xy[0][2]))
        b = (_parse_float(xy[1][1]), _parse_float(xy[1][2]))
        if None in a or None in b:
            continue
        if (_close(a[0], x1) and _close(a[1], y1) and _close(b[0], x2) and _close(b[1], y2)) or (
            _close(a[0], x2) and _close(a[1], y2) and _close(b[0], x1) and _close(b[1], y1)
        ):
            return wire
    return None


def _has_wire(schem: KicadSchematic, x1: float, y1: float, x2: float, y2: float) -> bool:
    return _find_wire(schem, x1, y1, x2, y2) is not None


def _has_label(schem: KicadSchematic, label_head: str, net_name: str, x: float, y: float) -> bool:
    return _find_label(schem, label_head, net_name, x, y) is not None


def clone_or_update_lib_symbol(
    schem: KicadSchematic,
    source_lib_id: str,
    target_lib_id: str,
    *,
    value: str,
    footprint: str,
    datasheet: str,
    description: str,
    reference: str = "U",
) -> bool:
    lib_symbols = schem.find_first("lib_symbols")
    if lib_symbols is None:
        return False

    target_entry = _find_lib_symbol_entry(lib_symbols, target_lib_id)
    if target_entry is None:
        source_entry = _find_lib_symbol_entry(lib_symbols, source_lib_id)
        if source_entry is None:
            return False
        target_entry = copy.deepcopy(source_entry)
        _rename_lib_symbol_tree(target_entry, source_lib_id, target_lib_id)
        lib_symbols.append(target_entry)

    _set_lib_symbol_property(target_entry, "Reference", reference)
    _set_lib_symbol_property(target_entry, "Value", value)
    _set_lib_symbol_property(target_entry, "Footprint", footprint)
    _set_lib_symbol_property(target_entry, "Datasheet", datasheet)
    _set_lib_symbol_property(target_entry, "Description", description)
    return True


def _has_no_connect(schem: KicadSchematic, x: float, y: float) -> bool:
    for marker in schem.top_level_nodes("no_connect"):
        at = _find_at(marker)
        if at is None:
            continue
        if _close(at[0], x) and _close(at[1], y):
            return True
    return False


def _new_wire(x1: float, y1: float, x2: float, y2: float) -> List[SExp]:
    return [
        atom("wire"),
        [atom("pts"), [atom("xy"), atom(_fmt(x1)), atom(_fmt(y1))], [atom("xy"), atom(_fmt(x2)), atom(_fmt(y2))]],
        [atom("stroke"), [atom("width"), atom("0")], [atom("type"), atom("default")]],
        [atom("uuid"), qstr(str(uuid.uuid4()))],
    ]


def _new_label(label_head: str, net_name: str, x: float, y: float, angle: float = 0.0) -> List[SExp]:
    justify = label_justify_for_angle(angle)
    base: List[SExp] = [
        atom(label_head),
        qstr(net_name),
        [atom("at"), atom(_fmt(x)), atom(_fmt(y)), atom(_fmt(angle))],
        [atom("effects"), [atom("font"), [atom("size"), atom("1.27"), atom("1.27")]], [atom("justify"), atom(justify)]],
        [atom("uuid"), qstr(str(uuid.uuid4()))],
    ]
    if label_head in {"global_label", "hierarchical_label"}:
        base.insert(2, [atom("shape"), atom("bidirectional")])
        base.insert(4, [atom("fields_autoplaced"), atom("yes")])

    if label_head == "global_label":
        intersheet_x = intersheet_x_for_label(x, angle)
        base.append(
            [
                atom("property"),
                qstr("Intersheetrefs"),
                qstr("${INTERSHEET_REFS}"),
                [atom("at"), atom(_fmt(intersheet_x)), atom(_fmt(y)), atom("0")],
                [atom("effects"), [atom("font"), [atom("size"), atom("1.27"), atom("1.27")]], [atom("hide"), atom("yes")]],
            ]
        )
    return base


def _replace_label_node(existing: List[SExp], replacement: List[SExp]) -> None:
    uuid_node = find_child(existing, "uuid")
    if uuid_node is not None:
        repl_uuid = find_child(replacement, "uuid")
        if repl_uuid is not None:
            repl_uuid[1] = uuid_node[1]
    existing[:] = replacement


def _find_label(schem: KicadSchematic, label_head: str, net_name: str, x: float, y: float) -> Optional[List[SExp]]:
    for label in schem.top_level_nodes(label_head):
        if _node_label_value(label) != net_name:
            continue
        at = _find_at(label)
        if at is None:
            continue
        if _close(at[0], x) and _close(at[1], y):
            return label
    return None


def _find_any_label_for_net(schem: KicadSchematic, net_name: str, x: float, y: float) -> Optional[List[SExp]]:
    for head in ("label", "hierarchical_label", "global_label"):
        label = _find_label(schem, head, net_name, x, y)
        if label is not None:
            return label
    return None


def _find_any_label_at_position(schem: KicadSchematic, x: float, y: float) -> Optional[List[SExp]]:
    for head in ("label", "hierarchical_label", "global_label"):
        for label in schem.top_level_nodes(head):
            at = _find_at(label)
            if at is None:
                continue
            if _close(at[0], x) and _close(at[1], y):
                return label
    return None


def _remove_conflicting_labels(schem: KicadSchematic, desired_head: str, net_name: str, x: float, y: float) -> bool:
    removed = False
    for head in ("label", "hierarchical_label", "global_label"):
        if head == desired_head:
            continue
        label = _find_label(schem, head, net_name, x, y)
        if label is not None:
            schem.remove_top_level(label)
            removed = True
    return removed


def _remove_other_labels_at_position(
    schem: KicadSchematic,
    x: float,
    y: float,
    *,
    keep: Optional[List[SExp]] = None,
) -> bool:
    removed = False
    for head in ("label", "hierarchical_label", "global_label"):
        for label in list(schem.top_level_nodes(head)):
            if label is keep:
                continue
            at = _find_at(label)
            if at is None:
                continue
            if _close(at[0], x) and _close(at[1], y):
                schem.remove_top_level(label)
                removed = True
    return removed


def _normalized_angle(angle: float) -> int:
    return int(round(angle)) % 360


def _new_no_connect(x: float, y: float) -> List[SExp]:
    return [
        atom("no_connect"),
        [atom("at"), atom(_fmt(x)), atom(_fmt(y))],
        [atom("uuid"), qstr(str(uuid.uuid4()))],
    ]


def _close(a: float, b: float, eps: float = 0.02) -> bool:
    return abs(a - b) <= eps


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _find_lib_symbol_entry(lib_symbols: List[SExp], lib_id: str) -> Optional[List[SExp]]:
    for entry in child_lists(lib_symbols, "symbol"):
        if len(entry) >= 2 and as_text(entry[1]) == lib_id:
            return entry
    return None


def _rename_lib_symbol_tree(node: List[SExp], source_lib_id: str, target_lib_id: str) -> None:
    source_leaf = source_lib_id.split(":")[-1]
    target_leaf = target_lib_id.split(":")[-1]
    if len(node) >= 2 and list_head(node) == "symbol":
        current_name = as_text(node[1])
        if current_name == source_lib_id:
            node[1] = qstr(target_lib_id)
        elif current_name == source_leaf:
            node[1] = qstr(target_leaf)
        elif current_name.startswith(source_leaf + "_"):
            node[1] = qstr(target_leaf + current_name[len(source_leaf):])
    for child in child_lists(node):
        _rename_lib_symbol_tree(child, source_lib_id, target_lib_id)


def _set_lib_symbol_property(symbol: List[SExp], name: str, value: str) -> None:
    for prop in child_lists(symbol, "property"):
        if len(prop) >= 3 and as_text(prop[1]) == name:
            prop[2] = qstr(value)
            return
    symbol.append(
        [
            atom("property"),
            qstr(name),
            qstr(value),
            [atom("at"), atom("0"), atom("0"), atom("0")],
            [atom("effects"), [atom("font"), [atom("size"), atom("1.27"), atom("1.27")]], [atom("hide"), atom("yes")]],
        ]
    )
