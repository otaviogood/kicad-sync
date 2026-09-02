"""``kicadsync place`` — arrange PCB footprints into functional clusters.

Reads the design JSON only for its *comment group* structure (the
``// ------ <title>`` separators already splitting root_components) and the
board for everything physical: footprints, pads, nets, courtyards. No new
JSON fields are needed — placement intent is inferred:

  - every component belongs to the comment group its JSON entry sits under
    (sheet-instance components group by their sheet_id);
  - small parts ("satellites": passives, transistors, test points) are
    attracted to the pad of an in-group "anchor" (IC / connector / large
    part) that shares their most-specific net — a decoupling cap whose
    non-GND net touches exactly one IC pin lands next to that pin;
  - anchors are shelf-packed inside their group, satellites spiral in
    around their target pads (rotated to face them, courtyard-collision
    checked), groups become rectangular blocks arranged so that groups
    sharing many nets sit next to each other.

The result is a starting arrangement, not a finished layout: refine in
KiCad, lock (in the footprint properties) anything you want kept, re-run.
Locked footprints are never moved — a group containing one is translated
so its layout lines up with the locked part. Footprints that aren't in the
JSON at all (e.g. hand-added mounting holes) are left alone and treated as
obstacles.

Workflow: ``kicadsync push``, open the PCB editor, "Update PCB from
Schematic" (F8), save, then ``kicadsync place``.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .kicad_pcb_model import Footprint, KicadPcb, Pad, rotate_local
from .kicad_sch_model import KicadSchematic, child_lists, find_child, get_property_value
from .kicad_sch_sexp import as_text, atom, list_head, qstr

BBox = Tuple[float, float, float, float]

CLEARANCE = 0.30          # min courtyard-to-courtyard gap when packing, mm
GROUP_MARGIN = 2.0        # padding added around a finished group block, mm
BLOCK_GAP = 4.0           # min gap between group blocks, mm (--gap overrides)
ANCHOR_AREA_MM2 = 25.0    # parts at least this big are anchors regardless of pins
ANCHOR_MIN_PADS = 5       # parts with at least this many pads are anchors
ANCHOR_PREFIXES = {"U", "J", "SW", "K", "M", "X", "Y"}
SAT_RING_STEP = 0.6       # satellite search ring growth, mm
SAT_RING_MAX = 20.0       # give up and park beyond this radius, mm
GRID_SNAP = 0.05          # final coordinate snap, mm
LABEL_STRIP = 2.4         # blank strip at the top of a group block for its label, mm
LABEL_SIZE = 1.4          # group label glyph height, mm

# Group outlines/labels go on a documentation layer (not plotted in fab
# output) and carry a recognisable UUID prefix so re-runs can find and
# replace exactly the annotations kicadsync drew, touching nothing else.
ANNOTATION_LAYER = "Cmts.User"
ANNOTATION_LAYER_NUMBER = 19
ANNOTATION_LAYER_DESC = "User.Comments"
ANNOTATION_UUID_PREFIX = "c0de9ace"


# ---------------------------------------------------------------------------
# Comment-group extraction: shared with the schematic generator — the JSON's
# `// ---- title` separators drive both the PCB clusters and the schematic
# group blocks.
# ---------------------------------------------------------------------------

from .json_schem_sync import extract_comment_groups  # noqa: E402  (re-exported)


# ---------------------------------------------------------------------------
# Board reference -> (json refdes, sheet instance) mapping
# ---------------------------------------------------------------------------

def reference_map_from_schematic(sch_path: Path) -> Dict[str, Tuple[str, Optional[str]]]:
    """Fallback for boards whose footprints carry no JsonRefdes property:
    recover reference -> (JsonRefdes, instance_name) from the schematic."""
    mapping: Dict[str, Tuple[str, Optional[str]]] = {}
    root = KicadSchematic.from_file(sch_path)

    for sym in root.top_level_nodes("symbol"):
        json_refdes = get_property_value(sym, "JsonRefdes")
        reference = get_property_value(sym, "Reference")
        if json_refdes and reference:
            mapping[reference] = (json_refdes, None)

    sheet_names: Dict[str, str] = {}
    sheet_files: Set[str] = set()
    for sheet in root.top_level_nodes("sheet"):
        uuid_node = find_child(sheet, "uuid")
        name = get_property_value(sheet, "Sheetname") or get_property_value(sheet, "Sheet name")
        file = get_property_value(sheet, "Sheetfile") or get_property_value(sheet, "Sheet file")
        if uuid_node is not None and len(uuid_node) >= 2 and name and file:
            sheet_names[as_text(uuid_node[1])] = name
            sheet_files.add(file)

    for file in sorted(sheet_files):
        child_path = sch_path.parent / file
        if not child_path.exists():
            continue
        child = KicadSchematic.from_file(child_path)
        for sym in child.top_level_nodes("symbol"):
            json_refdes = get_property_value(sym, "JsonRefdes")
            if not json_refdes:
                continue
            instances = find_child(sym, "instances")
            if instances is None:
                continue
            for project in child_lists(instances, "project"):
                for path_node in child_lists(project, "path"):
                    if len(path_node) < 2:
                        continue
                    sheet_uuid = as_text(path_node[1]).rstrip("/").split("/")[-1]
                    ref_node = find_child(path_node, "reference")
                    if ref_node is None or len(ref_node) < 2:
                        continue
                    reference = as_text(ref_node[1])
                    instance = sheet_names.get(sheet_uuid)
                    if instance:
                        mapping[reference] = (json_refdes, instance)
    return mapping


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Part:
    fp: Footprint
    ref: str
    json_refdes: str
    instance: Optional[str]
    group: str
    anchor: bool = False
    fixed: bool = False                      # locked in KiCad: never moved
    rot: float = 0.0
    local: Optional[Tuple[float, float]] = None   # group-local position
    target: Optional[Tuple[int, int]] = None      # (part index, pad index)
    target_net: Optional[str] = None
    depth: int = 0                                # 0 anchor, 1 sat-of-anchor, ...

    def bbox_local(self) -> BBox:
        return self.fp.bbox_at(self.rot)


def _ref_prefix(refdes: str) -> str:
    match = re.match(r"^([A-Za-z]+)", refdes)
    return match.group(1).upper() if match else "U"


def _bbox_area(box: BBox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _bbox_at(box: BBox, pos: Tuple[float, float]) -> BBox:
    return (box[0] + pos[0], box[1] + pos[1], box[2] + pos[0], box[3] + pos[1])


def _overlaps(a: BBox, b: BBox, gap: float) -> bool:
    return not (
        a[2] + gap <= b[0] or b[2] + gap <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


def _snap(value: float) -> float:
    return round(value / GRID_SNAP) * GRID_SNAP


def classify_anchor(json_refdes: str, fp: Footprint) -> bool:
    prefix = _ref_prefix(json_refdes)
    if prefix in ANCHOR_PREFIXES:
        return True
    pads = [p for p in fp.pads() if p.number]
    if len(pads) >= ANCHOR_MIN_PADS:
        return True
    # Inductors stay satellites no matter their size: they belong AT a
    # converter's SW pin, which ring placement gives them and shelf packing
    # would not.
    if prefix in ("L", "FB"):
        return False
    return _bbox_area(fp.local_bbox()) >= ANCHOR_AREA_MM2


# ---------------------------------------------------------------------------
# Connectivity: satellite -> anchor pad targeting
# ---------------------------------------------------------------------------

def _build_net_map(parts: Sequence[Part]) -> Dict[str, List[Tuple[int, int]]]:
    nets: Dict[str, List[Tuple[int, int]]] = {}
    for pi, part in enumerate(parts):
        for gi, pad in enumerate(part.fp.pads()):
            if not pad.net_name or not pad.number:
                continue
            nets.setdefault(pad.net_name, []).append((pi, gi))
    return nets


def _net_size(pcb: KicadPcb) -> Dict[str, int]:
    """Pad count per net across the WHOLE board (including unmatched
    footprints) so 'big' nets rank as unspecific even in small groups."""
    counts: Dict[str, int] = {}
    for fp in pcb.footprints():
        for pad in fp.pads():
            if pad.net_name:
                counts[pad.net_name] = counts.get(pad.net_name, 0) + 1
    return counts


_ANCHOR_CLASS_RANK = {"U": 0, "J": 1, "SW": 2}


def resolve_targets(parts: List[Part], nets: Dict[str, List[Tuple[int, int]]],
                    net_size: Dict[str, int]) -> None:
    """Attach every satellite to the in-group pad sharing its most-specific
    net. Chains (R -> C -> pin) resolve over multiple passes by letting a
    satellite target an already-resolved satellite."""

    def candidate_nets(part: Part) -> List[str]:
        names = {p.net_name for p in part.fp.pads() if p.net_name}
        return sorted(names, key=lambda n: (net_size.get(n, 0), n))

    def pick(part_index: int, allow_satellites: bool) -> Optional[Tuple[int, int, str]]:
        part = parts[part_index]
        for net in candidate_nets(part):
            best: Optional[Tuple[Tuple[int, int, str], int, int]] = None
            for pi, gi in nets.get(net, []):
                other = parts[pi]
                if pi == part_index or other.group != part.group:
                    continue
                if other.anchor:
                    rank = _ANCHOR_CLASS_RANK.get(_ref_prefix(other.json_refdes), 3)
                elif allow_satellites and other.target is not None:
                    rank = 8 + other.depth
                else:
                    continue
                key = (rank, pi)
                if best is None or key < best[1:]:
                    best = ((pi, gi, net), rank, pi)
            if best is not None:
                return best[0]
        return None

    satellite_order = [i for i, p in enumerate(parts) if not p.anchor and not p.fixed]
    for index in satellite_order:
        found = pick(index, allow_satellites=False)
        if found:
            pi, gi, net = found
            parts[index].target = (pi, gi)
            parts[index].target_net = net
            parts[index].depth = 1

    for _ in range(3):
        changed = False
        for index in satellite_order:
            if parts[index].target is not None:
                continue
            found = pick(index, allow_satellites=True)
            if found:
                pi, gi, net = found
                parts[index].target = (pi, gi)
                parts[index].target_net = net
                parts[index].depth = parts[pi].depth + 1
                changed = True
        if not changed:
            break


# ---------------------------------------------------------------------------
# Within-group layout (group-local coordinates)
# ---------------------------------------------------------------------------

def _halo_width(box: BBox, satellite_area: float) -> float:
    w = box[2] - box[0]
    h = box[3] - box[1]
    if satellite_area <= 0:
        return 1.0
    # ring area (W+2w)(H+2w) - WH = satellite_area  =>  4w^2 + 2(W+H)w - A = 0
    a = satellite_area
    width = (-(w + h) + math.sqrt((w + h) ** 2 + 4 * a)) / 4
    return max(1.0, width)


def _shelf_pack(cells: List[Tuple[float, float]], row_width: float) -> List[Tuple[float, float]]:
    """Pack (w, h) cells into rows; returns top-left positions."""
    positions: List[Tuple[float, float]] = []
    x = y = row_height = 0.0
    for w, h in cells:
        if x > 0 and x + w > row_width:
            y += row_height + CLEARANCE
            x = 0.0
            row_height = 0.0
        positions.append((x, y))
        x += w + CLEARANCE
        row_height = max(row_height, h)
    return positions


def _ring_positions(center: Tuple[float, float], prefer: Tuple[float, float],
                    r_start: float) -> Iterable[Tuple[float, float]]:
    prefer_angle = math.atan2(prefer[1], prefer[0]) if (prefer[0] or prefer[1]) else 0.0
    r = max(0.4, r_start)
    while r <= SAT_RING_MAX:
        count = max(8, int(2 * math.pi * r / 0.8))
        step = 2 * math.pi / count
        # Sweep outward from the preferred direction, alternating sides.
        for j in range(count):
            angle = prefer_angle + ((j + 1) // 2) * step * (1 if j % 2 == 0 else -1)
            yield (center[0] + r * math.cos(angle), center[1] + r * math.sin(angle))
        r += SAT_RING_STEP


def _pad_local_offset(part: Part, pad: Pad) -> Tuple[float, float]:
    return rotate_local(pad.x, pad.y, part.rot)


def _placed_pad_pos(part: Part, pad_index: int) -> Tuple[float, float]:
    pad = part.fp.pads()[pad_index]
    off = _pad_local_offset(part, pad)
    assert part.local is not None
    return (part.local[0] + off[0], part.local[1] + off[1])


def layout_group(parts: List[Part], members: List[int]) -> Tuple[float, float]:
    """Assign group-local positions to `members`; returns (width, height)."""
    anchors = [i for i in members if parts[i].anchor]
    satellites = [i for i in members if not parts[i].anchor]

    sat_area_by_anchor: Dict[int, float] = {i: 0.0 for i in anchors}
    loose: List[int] = []
    for si in satellites:
        root = _chain_root(parts, si)
        if root is not None and root in sat_area_by_anchor:
            sat_area_by_anchor[root] += _bbox_area(parts[si].bbox_local()) * 2.2
        else:
            loose.append(si)

    # Anchors: biggest/busiest first, each padded by a halo sized for its sats.
    anchors.sort(key=lambda i: (-len(parts[i].fp.pads()), -_bbox_area(parts[i].bbox_local()), parts[i].ref))
    cells: List[Tuple[float, float]] = []
    halos: List[float] = []
    for i in anchors:
        box = parts[i].bbox_local()
        halo = _halo_width(box, sat_area_by_anchor.get(i, 0.0))
        halos.append(halo)
        cells.append((box[2] - box[0] + 2 * halo, box[3] - box[1] + 2 * halo))
    total_area = sum(w * h for w, h in cells) or 1.0
    row_width = max([math.sqrt(total_area) * 1.15] + [w for w, _ in cells])
    cell_pos = _shelf_pack(cells, row_width)
    for i, halo, (cx, cy) in zip(anchors, halos, cell_pos):
        box = parts[i].bbox_local()
        parts[i].local = (cx + halo - box[0], cy + halo - box[1])

    obstacles: List[BBox] = []
    for i in anchors:
        assert parts[i].local is not None
        obstacles.extend(_bbox_at(r, parts[i].local) for r in parts[i].fp.rects_at(parts[i].rot))

    # Satellites: chain depth first, then most-specific nets closest in.
    ordered = sorted(
        (si for si in satellites if si not in loose),
        key=lambda si: (parts[si].depth, _target_net_size_key(parts, si), parts[si].ref),
    )
    for si in ordered:
        part = parts[si]
        assert part.target is not None
        holder = parts[part.target[0]]
        if holder.local is None:
            loose.append(si)
            continue
        target = _placed_pad_pos(holder, part.target[1])
        holder_box = _bbox_at(holder.bbox_local(), holder.local)
        holder_center = ((holder_box[0] + holder_box[2]) / 2, (holder_box[1] + holder_box[3]) / 2)
        prefer = (target[0] - holder_center[0], target[1] - holder_center[1])
        own_pad = _own_attracted_pad(part)
        box = part.fp.local_bbox()
        r0 = 0.5 * math.hypot(box[2] - box[0], box[3] - box[1]) + 0.3
        placed = False
        for pos in _ring_positions(target, prefer, r0):
            rot = _best_rotation(part, own_pad, pos, target)
            candidate_box = _bbox_at(part.fp.bbox_at(rot), pos)
            if any(_overlaps(candidate_box, other, CLEARANCE) for other in obstacles):
                continue
            part.rot = rot
            part.local = pos
            obstacles.extend(_bbox_at(r, pos) for r in part.fp.rects_at(rot))
            placed = True
            break
        if not placed:
            loose.append(si)

    # Loose parts (no target found / no room): a tidy grid below the rest.
    if loose:
        base_y = max((b[3] for b in obstacles), default=0.0) + CLEARANCE + 0.7
        loose_cells = []
        for si in loose:
            box = parts[si].fp.local_bbox()
            loose_cells.append((box[2] - box[0], box[3] - box[1]))
        width = max((b[2] for b in obstacles), default=0.0)
        width = max(width, math.sqrt(sum(w * h for w, h in loose_cells)) * 1.4, 6.0)
        for si, (cx, cy) in zip(loose, _shelf_pack(loose_cells, width)):
            box = parts[si].fp.local_bbox()
            parts[si].local = (cx - box[0], cy + base_y - box[1])
            obstacles.append(_bbox_at(parts[si].bbox_local(), parts[si].local))

    # Normalise: content starts at (0, 0).
    min_x = min((b[0] for b in obstacles), default=0.0)
    min_y = min((b[1] for b in obstacles), default=0.0)
    max_x = max((b[2] for b in obstacles), default=0.0)
    max_y = max((b[3] for b in obstacles), default=0.0)
    for i in members:
        if parts[i].local is not None:
            parts[i].local = (parts[i].local[0] - min_x, parts[i].local[1] - min_y)
    return (max_x - min_x, max_y - min_y)


def _chain_root(parts: Sequence[Part], index: int) -> Optional[int]:
    seen = set()
    current = index
    while parts[current].target is not None and current not in seen:
        seen.add(current)
        current = parts[current].target[0]
    return current if parts[current].anchor else None


def _target_net_size_key(parts: Sequence[Part], index: int) -> int:
    part = parts[index]
    if part.target_net is None:
        return 1 << 30
    return sum(1 for p in parts for pad in p.fp.pads() if pad.net_name == part.target_net)


def _own_attracted_pad(part: Part) -> Pad:
    pads = [p for p in part.fp.pads() if p.number]
    for pad in pads:
        if pad.net_name == part.target_net:
            return pad
    return pads[0]


def _best_rotation(part: Part, own_pad: Pad, pos: Tuple[float, float],
                   target: Tuple[float, float]) -> float:
    best_rot = 0.0
    best_dist = float("inf")
    for rot in (0.0, 90.0, 180.0, 270.0):
        off = rotate_local(own_pad.x, own_pad.y, rot)
        dist = math.hypot(pos[0] + off[0] - target[0], pos[1] + off[1] - target[1])
        if dist < best_dist - 1e-9:
            best_dist = dist
            best_rot = rot
    return best_rot


# ---------------------------------------------------------------------------
# Board-level block layout
# ---------------------------------------------------------------------------

def _group_weights(parts: Sequence[Part], nets: Dict[str, List[Tuple[int, int]]],
                   groups: Sequence[str]) -> Dict[Tuple[str, str], int]:
    limit = max(2, math.ceil(len(groups) * 0.5))
    weights: Dict[Tuple[str, str], int] = {}
    for pads in nets.values():
        touched = sorted({parts[pi].group for pi, _ in pads})
        if len(touched) < 2 or len(touched) > limit:
            continue
        for i, g1 in enumerate(touched):
            for g2 in touched[i + 1:]:
                weights[(g1, g2)] = weights.get((g1, g2), 0) + 1
    return weights


def _weight(weights: Dict[Tuple[str, str], int], g1: str, g2: str) -> int:
    return weights.get((g1, g2), 0) + weights.get((g2, g1), 0)


def place_blocks(
    sizes: Dict[str, Tuple[float, float]],
    weights: Dict[Tuple[str, str], int],
    fixed_blocks: Dict[str, Tuple[float, float]],
    obstacles: List[BBox],
    board: Optional[BBox],
    origin: Tuple[float, float],
    gap: float = BLOCK_GAP,
) -> Dict[str, Tuple[float, float]]:
    """Greedy block placement: most-connected groups first, each at the spot
    minimising net-weighted distance to already-placed groups."""
    result: Dict[str, Tuple[float, float]] = dict(fixed_blocks)
    todo = [g for g in sorted(sizes) if g not in fixed_blocks]
    if board is None:
        area = sum((w + gap) * (h + gap) for w, h in sizes.values()) * 1.7
        width = math.sqrt(area * 1.35)
        height = area / width
        for g, (w, h) in sizes.items():
            width = max(width, w + 2)
            height = max(height, h + 2)
        board = (origin[0], origin[1], origin[0] + width, origin[1] + height)

    def block_box(g: str, pos: Tuple[float, float]) -> BBox:
        w, h = sizes[g]
        return (pos[0], pos[1], pos[0] + w, pos[1] + h)

    def total_weight(g: str) -> int:
        return sum(_weight(weights, g, other) for other in sizes if other != g)

    todo.sort(key=lambda g: (-total_weight(g), -sizes[g][0] * sizes[g][1], g))

    step = 2.0
    for g in list(todo):
        w, h = sizes[g]
        placed_boxes = [block_box(og, result[og]) for og in result] + obstacles
        best: Optional[Tuple[float, Tuple[float, float]]] = None
        cx_board = (board[0] + board[2]) / 2
        cy_board = (board[1] + board[3]) / 2
        x = board[0]
        while x + w <= board[2] + 1e-6:
            y = board[1]
            while y + h <= board[3] + 1e-6:
                box = (x, y, x + w, y + h)
                if not any(_overlaps(box, other, gap) for other in placed_boxes):
                    cx = x + w / 2
                    cy = y + h / 2
                    cost = 0.01 * math.hypot(cx - cx_board, cy - cy_board)
                    for og in result:
                        wt = _weight(weights, g, og)
                        if wt:
                            ob = block_box(og, result[og])
                            cost += wt * math.hypot(cx - (ob[0] + ob[2]) / 2, cy - (ob[1] + ob[3]) / 2)
                    if best is None or cost < best[0]:
                        best = (cost, (x, y))
                y += step
            x += step
        if best is None:
            # No room: extend below the board and keep going.
            max_y = max([b[3] for b in placed_boxes] + [board[1]])
            best = (0.0, (board[0], max_y + gap))
        result[g] = best[1]
    return result


# ---------------------------------------------------------------------------
# Group outlines + labels (documentation layer)
# ---------------------------------------------------------------------------

def _annotation_uuid(kind: str, key: str) -> str:
    """Stable UUID in the kicadsync namespace, so a re-run replaces exactly
    the annotations it drew last time."""
    digest = hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()
    return (
        f"{ANNOTATION_UUID_PREFIX}-{digest[0:4]}-4{digest[4:7]}-"
        f"8{digest[7:10]}-{digest[10:22]}"
    )


def _is_annotation(node: List[object]) -> bool:
    uuid_node = find_child(node, "uuid")  # type: ignore[arg-type]
    if uuid_node is None or len(uuid_node) < 2:
        return False
    return as_text(uuid_node[1]).startswith(ANNOTATION_UUID_PREFIX + "-")


def remove_group_annotations(pcb: KicadPcb) -> int:
    """Drop the outlines/labels from previous runs. Anything else on the
    layer — including drawings the user added — is left alone."""
    stale = [
        node for node in pcb.top_level_nodes()
        if list_head(node) in ("gr_rect", "gr_text") and _is_annotation(node)
    ]
    for node in stale:
        pcb.remove_top_level(node)
    return len(stale)


def draw_group_annotations(
    pcb: KicadPcb,
    sizes: Dict[str, Tuple[float, float]],
    block_pos: Dict[str, Tuple[float, float]],
    counts: Dict[str, int],
) -> None:
    """Draw a labelled rectangle around each group block."""
    pcb.ensure_layer(ANNOTATION_LAYER, ANNOTATION_LAYER_NUMBER, ANNOTATION_LAYER_DESC)
    for group in sorted(sizes):
        if group not in block_pos:
            continue
        x, y = block_pos[group]
        w, h = sizes[group]
        pcb.append_top_level([
            atom("gr_rect"),
            [atom("start"), atom(_fmt(x)), atom(_fmt(y))],
            [atom("end"), atom(_fmt(x + w)), atom(_fmt(y + h))],
            [atom("stroke"), [atom("width"), atom("0.15")], [atom("type"), atom("dash")]],
            [atom("fill"), atom("no")],
            [atom("layer"), qstr(ANNOTATION_LAYER)],
            [atom("uuid"), qstr(_annotation_uuid("rect", group))],
        ])
        label = f"{_group_label(group)}  ({counts.get(group, 0)})"
        pcb.append_top_level([
            atom("gr_text"), qstr(label),
            [atom("at"), atom(_fmt(x + 0.8)), atom(_fmt(y + LABEL_STRIP - 0.5)), atom("0")],
            [atom("layer"), qstr(ANNOTATION_LAYER)],
            [atom("uuid"), qstr(_annotation_uuid("text", group))],
            [atom("effects"),
             [atom("font"),
              [atom("size"), atom(_fmt(LABEL_SIZE)), atom(_fmt(LABEL_SIZE))],
              [atom("thickness"), atom("0.25")]],
             [atom("justify"), atom("left"), atom("bottom")]],
        ])


def _group_label(group: str) -> str:
    if group.startswith("sheet:"):
        return f"{group[len('sheet:'):]} (sheet)"
    return group


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class PlaceReport:
    groups: Dict[str, List[str]] = field(default_factory=dict)
    parked: List[str] = field(default_factory=list)
    locked: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    moved: int = 0


def build_parts(pcb: KicadPcb, json_path: Path, sch_path: Optional[Path]) -> Tuple[List[Part], List[Footprint], PlaceReport]:
    from . import json_schem_sync as sync

    raw = json_path.read_text(encoding="utf-8")
    comment_groups = extract_comment_groups(raw)
    design = sync.design_from_json(json_path)
    instance_to_sheet = {i.instance_name: i.sheet_id for i in design.sheet_instances}
    known_root = {c.refdes for c in design.root_components}
    known_sheet_refdes = {
        (sheet.sheet_id, comp.refdes)
        for sheet in design.sheet_definitions
        for comp in sheet.components
    }

    report = PlaceReport()
    fallback: Optional[Dict[str, Tuple[str, Optional[str]]]] = None
    parts: List[Part] = []
    unmatched: List[Footprint] = []

    for fp in pcb.footprints():
        json_refdes = fp.get_property("JsonRefdes")
        instance = fp.get_property("Sheetname")
        if instance is not None and instance not in instance_to_sheet:
            instance = None
        if json_refdes is None and fp.reference:
            if fallback is None:
                fallback = reference_map_from_schematic(sch_path) if sch_path and sch_path.exists() else {}
            json_refdes, instance = fallback.get(fp.reference, (None, None))

        group: Optional[str] = None
        if json_refdes:
            if instance and (instance_to_sheet.get(instance), json_refdes) in known_sheet_refdes:
                group = f"sheet:{instance_to_sheet[instance]}"
            elif json_refdes in known_root:
                group = comment_groups.get(json_refdes, "main")
            else:
                # Sheet component whose footprint lost its Sheetname property:
                # still group it with its sheet siblings.
                owners = sorted({sid for sid, ref in known_sheet_refdes if ref == json_refdes})
                if owners:
                    group = f"sheet:{owners[0]}"
        if group is None:
            unmatched.append(fp)
            report.unmatched.append(fp.reference or fp.lib_id)
            continue

        part = Part(
            fp=fp,
            ref=fp.reference,
            json_refdes=json_refdes,
            instance=instance,
            group=group,
            fixed=fp.locked,
        )
        part.anchor = classify_anchor(json_refdes, fp)
        # Re-runs respect hand rotation: anchors keep their current angle, and
        # a locked part's satellites plan around its true orientation.
        # Satellite rotation is recomputed from its target pad anyway.
        if part.anchor or part.fixed:
            part.rot = fp.at[2]
        if part.fixed:
            report.locked.append(part.ref)
        parts.append(part)
        report.groups.setdefault(group, []).append(part.ref)

    return parts, unmatched, report


def _warn_detached_zones(pcb: KicadPcb) -> List[str]:
    """Flag embedded zones (antenna keep-outs) that no longer touch their own
    footprint. set_at() moves a zone by the same delta as its footprint, so a
    zone stranded by an earlier edit keeps its wrong offset forever — it needs
    the KiCad-side repair, not another place run."""
    flagged: List[str] = []
    for fp in pcb.footprints():
        zones = fp.zone_bboxes()
        if not zones:
            continue
        x, y, rot = fp.at
        b = fp.bbox_at(rot)
        fp_box = (b[0] + x, b[1] + y, b[2] + x, b[3] + y)
        # -2.0: a legit keep-out may poke slightly past the courtyard; a
        # stranded one is typically tens of mm away.
        if any(not _overlaps(zb, fp_box, -2.0) for zb in zones):
            flagged.append(fp.reference or fp.lib_id)
    for ref in flagged:
        print(
            f"[place] WARNING: {ref} has an embedded keep-out zone detached from its "
            "footprint (stranded by an earlier edit). place preserves the offset when "
            "moving it — repair in KiCad: select it, Tools -> Update Footprints from Library..."
        )
    return flagged


def run_place(
    pcb_path: Path,
    json_path: Path,
    sch_path: Optional[Path],
    board_size: Optional[Tuple[float, float]] = None,
    origin: Tuple[float, float] = (30.0, 30.0),
    gap: float = BLOCK_GAP,
    outlines: bool = True,
    dry_run: bool = False,
) -> PlaceReport:
    pcb = KicadPcb.from_file(pcb_path)
    parts, unmatched, report = build_parts(pcb, json_path, sch_path)
    if not parts:
        raise SystemExit(
            f"[place] no matching footprints in {pcb_path.name} — run KiCad's "
            "'Update PCB from Schematic' (F8) first."
        )

    removed = remove_group_annotations(pcb)
    _warn_detached_zones(pcb)

    nets = _build_net_map(parts)
    net_size = _net_size(pcb)
    resolve_targets(parts, nets, net_size)

    group_names = sorted({p.group for p in parts})
    members_by_group = {g: [i for i, p in enumerate(parts) if p.group == g] for g in group_names}

    sizes: Dict[str, Tuple[float, float]] = {}
    for g in group_names:
        w, h = layout_group(parts, members_by_group[g])
        sizes[g] = (w + 2 * GROUP_MARGIN, h + 2 * GROUP_MARGIN + LABEL_STRIP)

    # Groups pinned by a locked member keep that member where it is.
    fixed_blocks: Dict[str, Tuple[float, float]] = {}
    for g in group_names:
        locked = [i for i in members_by_group[g] if parts[i].fixed and parts[i].local is not None]
        if not locked:
            continue
        locked.sort(key=lambda i: (not parts[i].anchor, parts[i].ref))
        pin = parts[locked[0]]
        actual = pin.fp.at
        assert pin.local is not None
        fixed_blocks[g] = (
            actual[0] - pin.local[0] - GROUP_MARGIN,
            actual[1] - pin.local[1] - GROUP_MARGIN - LABEL_STRIP,
        )
        if len(locked) > 1:
            print(f"[place] note: group '{g}' has {len(locked)} locked parts; aligned to {pin.ref}")

    obstacles = []
    for fp in unmatched:
        x, y, rot = fp.at
        obstacles.append(_bbox_at(fp.bbox_at(rot), (x, y)))

    board = pcb.edge_cuts_bbox()
    if board is not None:
        board = (board[0] + 1.0, board[1] + 1.0, board[2] - 1.0, board[3] - 1.0)
    elif board_size is not None:
        board = (origin[0], origin[1], origin[0] + board_size[0], origin[1] + board_size[1])

    block_pos = place_blocks(sizes, _group_weights(parts, nets, group_names),
                             fixed_blocks, obstacles, board, origin, gap=gap)

    for g in group_names:
        gx, gy = block_pos[g]
        for i in members_by_group[g]:
            part = parts[i]
            if part.fixed or part.local is None:
                continue
            x = _snap(gx + GROUP_MARGIN + part.local[0])
            y = _snap(gy + GROUP_MARGIN + LABEL_STRIP + part.local[1])
            part.fp.set_at(x, y, part.rot)
            report.moved += 1
        for i in members_by_group[g]:
            if parts[i].target is None and not parts[i].anchor and not parts[i].fixed:
                report.parked.append(parts[i].ref)

    if outlines:
        draw_group_annotations(pcb, sizes, block_pos,
                               {g: len(refs) for g, refs in report.groups.items()})
        print(f"[place] drew {len(sizes)} group outlines + labels on {ANNOTATION_LAYER}"
              + (f" (replacing {removed} from the previous run)" if removed else ""))
    elif removed:
        print(f"[place] removed {removed} group outlines/labels from the previous run")

    _print_report(report, sizes, block_pos, parts)
    if dry_run:
        print("[place] dry run — board not written")
    else:
        pcb.to_file(pcb_path)
        print(f"[place] wrote {pcb_path}")
    return report


def _print_report(report: PlaceReport, sizes: Dict[str, Tuple[float, float]],
                  block_pos: Dict[str, Tuple[float, float]], parts: Sequence[Part]) -> None:
    print(f"[place] {report.moved} footprints placed in {len(report.groups)} groups:")
    for g in sorted(report.groups):
        w, h = sizes.get(g, (0.0, 0.0))
        x, y = block_pos.get(g, (0.0, 0.0))
        anchors = sum(1 for p in parts if p.group == g and p.anchor)
        sats = sum(1 for p in parts if p.group == g and not p.anchor)
        print(f"  {g:<42} {len(report.groups[g]):>3} parts ({anchors} anchors, {sats} satellites) "
              f"{w:.0f}x{h:.0f}mm at ({x:.0f}, {y:.0f})")
    if report.parked:
        print(f"[place] parked (no net target found): {', '.join(sorted(report.parked))}")
    if report.locked:
        print(f"[place] locked, not moved: {', '.join(sorted(report.locked))}")
    if report.unmatched:
        print(f"[place] not in design JSON, left alone: {', '.join(sorted(report.unmatched))}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kicadsync place",
        description="Cluster and place PCB footprints from design-JSON groups + net connectivity.",
    )
    parser.add_argument("--json", required=True, help="design JSON path")
    parser.add_argument("--pcb", required=True, help=".kicad_pcb path (after Update PCB from Schematic)")
    parser.add_argument("--sch", help="root schematic (fallback refdes mapping)")
    parser.add_argument("--board", help="board size WxH in mm when there is no Edge.Cuts outline (e.g. 80x100)")
    parser.add_argument("--origin", default="30,30", help="top-left placement origin in mm (default 30,30)")
    parser.add_argument("--gap", type=float, default=BLOCK_GAP,
                        help=f"minimum space between group blocks in mm (default {BLOCK_GAP:g})")
    parser.add_argument("--no-outlines", action="store_true",
                        help="don't draw the labelled group boxes (also clears ones drawn before)")
    parser.add_argument("--dry-run", action="store_true", help="report the plan without writing the board")
    args = parser.parse_args(argv)

    board_size = None
    if args.board:
        match = re.match(r"^\s*([\d.]+)\s*[xX,]\s*([\d.]+)\s*$", args.board)
        if not match:
            raise SystemExit(f"[place] --board must look like 80x100, got {args.board!r}")
        board_size = (float(match.group(1)), float(match.group(2)))
    match = re.match(r"^\s*([\d.]+)\s*,\s*([\d.]+)\s*$", args.origin)
    if not match:
        raise SystemExit(f"[place] --origin must look like 30,30, got {args.origin!r}")
    origin = (float(match.group(1)), float(match.group(2)))

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        raise SystemExit(
            f"[place] {pcb_path} does not exist. Open the project in KiCad's PCB editor, "
            "run 'Update PCB from Schematic' (F8), save, then re-run kicadsync place."
        )
    run_place(
        pcb_path=pcb_path,
        json_path=Path(args.json),
        sch_path=Path(args.sch) if args.sch else None,
        board_size=board_size,
        origin=origin,
        gap=args.gap,
        outlines=not args.no_outlines,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
