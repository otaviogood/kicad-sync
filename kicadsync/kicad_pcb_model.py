"""Native KiCad PCB (.kicad_pcb) model — the board-side sibling of
kicad_sch_model.

Parses the whole board into generic S-expressions and offers a thin
``Footprint`` accessor for the parts placement cares about: reference,
properties, position/rotation, pads (with nets), and courtyard bounding
boxes. Everything not understood is preserved on round-trip; edits are
surgical (only ``(at ...)`` nodes are rewritten when moving a footprint).

KiCad geometry conventions used here:
  - board coordinates are millimetres, +y points DOWN;
  - footprint/pad angles are degrees, counter-clockwise on screen;
  - pad ``(at x y a)`` positions are footprint-local (unrotated), but the
    stored pad ANGLE includes the footprint's own rotation, so changing a
    footprint's rotation must add the delta to every child angle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .kicad_sch_model import child_lists, find_child, get_property_value
from .kicad_sch_sexp import Atom, SExp, as_text, atom, dump_many, list_head, parse_one, qstr


@dataclass
class KicadPcb:
    root: List[SExp]

    @classmethod
    def from_text(cls, text: str) -> "KicadPcb":
        node = parse_one(text)
        if not isinstance(node, list) or list_head(node) != "kicad_pcb":
            raise ValueError("Expected top-level (kicad_pcb ...)")
        return cls(root=node)

    @classmethod
    def from_file(cls, path: Path) -> "KicadPcb":
        return cls.from_text(path.read_text(encoding="utf-8"))

    def to_text(self) -> str:
        return dump_many([self.root]) + "\n"

    def to_file(self, path: Path) -> None:
        path.write_text(self.to_text(), encoding="utf-8")

    def top_level_nodes(self, head: Optional[str] = None) -> List[List[SExp]]:
        out: List[List[SExp]] = []
        for child in self.root[1:]:
            if not isinstance(child, list):
                continue
            if head is None or list_head(child) == head:
                out.append(child)
        return out

    def append_top_level(self, node: List[SExp]) -> None:
        self.root.append(node)

    def remove_top_level(self, node: List[SExp]) -> None:
        self.root.remove(node)

    def ensure_layer(self, name: str, canonical: int, description: str) -> None:
        """Declare a layer in the (layers ...) block if the board lacks it.

        Real boards declare every layer already; this covers hand-made or
        minimal files so writing to a documentation layer never produces a
        board KiCad refuses to open.
        """
        layers = None
        for node in self.top_level_nodes("layers"):
            layers = node
            break
        if layers is None:
            return
        used: List[int] = []
        for entry in layers[1:]:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            if as_text(entry[1]) == name:
                return
            try:
                used.append(int(as_text(entry[0])))
            except ValueError:
                continue
        number = canonical if canonical not in used else (max(used, default=0) + 1)
        layers.append([atom(str(number)), qstr(name), atom("user"), qstr(description)])

    def footprints(self) -> List["Footprint"]:
        return [Footprint(node) for node in self.top_level_nodes("footprint")]

    def edge_cuts_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """(min_x, min_y, max_x, max_y) of Edge.Cuts graphics, if any."""
        pts: List[Tuple[float, float]] = []
        for node in self.top_level_nodes():
            if list_head(node) not in (
                "gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve",
            ):
                continue
            if _node_layer(node) != "Edge.Cuts":
                continue
            pts.extend(_graphic_points(node))
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


def _floats(node: List[SExp], start: int = 1) -> List[float]:
    out: List[float] = []
    for item in node[start:]:
        if isinstance(item, list):
            continue
        try:
            out.append(float(as_text(item)))
        except ValueError:
            continue
    return out


def _node_layer(node: List[SExp]) -> Optional[str]:
    layer = find_child(node, "layer")
    if layer is not None and len(layer) >= 2:
        return as_text(layer[1])
    return None


def _graphic_points(node: List[SExp]) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for head in ("start", "end", "mid", "center"):
        child = find_child(node, head)
        if child is not None:
            vals = _floats(child)
            if len(vals) >= 2:
                pts.append((vals[0], vals[1]))
    pts_node = find_child(node, "pts")
    if pts_node is not None:
        for xy in child_lists(pts_node, "xy"):
            vals = _floats(xy)
            if len(vals) >= 2:
                pts.append((vals[0], vals[1]))
    # A circle is (center)+(end); represent it by its bounding square.
    if list_head(node) == "gr_circle" or list_head(node) == "fp_circle":
        center = find_child(node, "center")
        end = find_child(node, "end")
        if center is not None and end is not None:
            c = _floats(center)
            e = _floats(end)
            if len(c) >= 2 and len(e) >= 2:
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                pts = [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    return pts


def _fmt_num(value: float) -> str:
    """KiCad-style number: no trailing zeros, no trailing dot."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


@dataclass
class Pad:
    number: str
    x: float          # footprint-local, unrotated
    y: float
    size_x: float
    size_y: float
    net_name: Optional[str]
    through_hole: bool


def rotate_local(px: float, py: float, angle_deg: float) -> Tuple[float, float]:
    """Footprint-local -> board offset for a footprint rotated angle_deg CCW
    (y-down board coordinates)."""
    a = math.radians(angle_deg)
    return (
        px * math.cos(a) + py * math.sin(a),
        -px * math.sin(a) + py * math.cos(a),
    )


class Footprint:
    def __init__(self, node: List[SExp]):
        self.node = node

    @property
    def lib_id(self) -> str:
        if len(self.node) >= 2 and not isinstance(self.node[1], list):
            return as_text(self.node[1])
        return ""

    def get_property(self, name: str) -> Optional[str]:
        return get_property_value(self.node, name)

    @property
    def reference(self) -> str:
        ref = self.get_property("Reference")
        if ref is not None:
            return ref
        # Very old boards use (fp_text reference "R1" ...)
        for text in child_lists(self.node, "fp_text"):
            if len(text) >= 3 and as_text(text[1]) == "reference":
                return as_text(text[2])
        return ""

    @property
    def layer(self) -> str:
        return _node_layer(self.node) or "F.Cu"

    @property
    def locked(self) -> bool:
        lock = find_child(self.node, "locked")
        if lock is not None and len(lock) >= 2:
            return as_text(lock[1]) != "no"
        return any(isinstance(item, Atom) and item.value == "locked" for item in self.node[1:])

    @property
    def at(self) -> Tuple[float, float, float]:
        node = find_child(self.node, "at")
        vals = _floats(node) if node is not None else []
        x = vals[0] if len(vals) >= 1 else 0.0
        y = vals[1] if len(vals) >= 2 else 0.0
        rot = vals[2] if len(vals) >= 3 else 0.0
        return (x, y, rot)

    def set_at(self, x: float, y: float, rot: float = 0.0) -> None:
        """Move/rotate the footprint, keeping child geometry consistent.

        Two child conventions have to be maintained by hand:
          - pad/text angles are stored ABSOLUTE (the footprint's rotation is
            baked in), so a rotation change is added to every (at ... a);
          - an embedded (zone ...) — e.g. the ESP32 module's antenna
            keep-out — stores its polygon in ABSOLUTE BOARD coordinates, so
            its points must be re-derived from the new placement or the zone
            is left behind when the footprint moves.
        """
        old_x, old_y, old_rot = self.at
        delta = (rot - old_rot) % 360.0
        self._move_zones((old_x, old_y), old_rot, (x, y), rot)
        at_node = find_child(self.node, "at")
        new_at: List[SExp] = [atom("at"), atom(_fmt_num(x)), atom(_fmt_num(y))]
        if rot % 360.0 != 0.0:
            new_at.append(atom(_fmt_num(rot % 360.0)))
        if at_node is None:
            self.node.insert(2, new_at)
        else:
            at_node[:] = new_at
        if delta == 0.0:
            return
        for child in self.node[1:]:
            if not isinstance(child, list):
                continue
            if list_head(child) in ("pad", "fp_text", "property"):
                _rotate_child_at(child, delta)

    def pads(self) -> List[Pad]:
        out: List[Pad] = []
        for pad in child_lists(self.node, "pad"):
            number = as_text(pad[1]) if len(pad) >= 2 else ""
            pad_type = as_text(pad[2]) if len(pad) >= 3 and not isinstance(pad[2], list) else ""
            at_node = find_child(pad, "at")
            vals = _floats(at_node) if at_node is not None else []
            size_node = find_child(pad, "size")
            size = _floats(size_node) if size_node is not None else []
            net_node = find_child(pad, "net")
            net_name: Optional[str] = None
            if net_node is not None and len(net_node) >= 3:
                net_name = as_text(net_node[2])
            out.append(
                Pad(
                    number=number,
                    x=vals[0] if len(vals) >= 1 else 0.0,
                    y=vals[1] if len(vals) >= 2 else 0.0,
                    size_x=size[0] if len(size) >= 1 else 0.0,
                    size_y=size[1] if len(size) >= 2 else 0.0,
                    net_name=net_name,
                    through_hole=pad_type in ("thru_hole", "np_thru_hole"),
                )
            )
        return out

    def local_bbox(self) -> Tuple[float, float, float, float]:
        """Courtyard bbox in footprint-local (unrotated) coordinates; falls
        back to pads (inflated) when no courtyard is drawn."""
        pts: List[Tuple[float, float]] = []
        for child in self.node[1:]:
            if not isinstance(child, list):
                continue
            if list_head(child) not in (
                "fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly", "fp_curve",
            ):
                continue
            if _node_layer(child) not in ("F.CrtYd", "B.CrtYd"):
                continue
            pts.extend(_graphic_points(child))
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        pads = self.pads()
        if pads:
            margin = 0.25
            return (
                min(p.x - p.size_x / 2 for p in pads) - margin,
                min(p.y - p.size_y / 2 for p in pads) - margin,
                max(p.x + p.size_x / 2 for p in pads) + margin,
                max(p.y + p.size_y / 2 for p in pads) + margin,
            )
        return (-0.5, -0.5, 0.5, 0.5)

    def bbox_at(self, rot: float) -> Tuple[float, float, float, float]:
        """Axis-aligned local bbox after rotating the footprint rot degrees."""
        x0, y0, x1, y1 = self.local_bbox()
        corners = [rotate_local(x, y, rot) for x in (x0, x1) for y in (y0, y1)]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    def _move_zones(self, old_pos: Tuple[float, float], old_rot: float,
                    new_pos: Tuple[float, float], new_rot: float) -> None:
        """Re-place absolute zone geometry for a footprint moving from
        (old_pos, old_rot) to (new_pos, new_rot)."""
        if old_pos == new_pos and old_rot == new_rot:
            return
        for zone in child_lists(self.node, "zone"):
            for xy in _iter_xy(zone):
                vals = _floats(xy)
                if len(vals) < 2:
                    continue
                # absolute -> footprint-local -> absolute again
                lx, ly = rotate_local(vals[0] - old_pos[0], vals[1] - old_pos[1], -old_rot)
                dx, dy = rotate_local(lx, ly, new_rot)
                xy[:] = [atom("xy"), atom(_fmt_num(new_pos[0] + dx)),
                         atom(_fmt_num(new_pos[1] + dy))]

    def zone_bboxes(self) -> List[Tuple[float, float, float, float]]:
        """Absolute bounding boxes of any embedded zones (keep-outs)."""
        out: List[Tuple[float, float, float, float]] = []
        for zone in child_lists(self.node, "zone"):
            pts = [_floats(xy) for xy in _iter_xy(zone)]
            pts = [p for p in pts if len(p) >= 2]
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append((min(xs), min(ys), max(xs), max(ys)))
        return out

    def courtyard_rects(self) -> List[Tuple[float, float, float, float]]:
        """The courtyard as a set of local rectangles.

        Non-rectangular courtyards (the ESP32 module's body + antenna
        keep-out "T", for example) decompose into grid cells that lie
        inside the outline, so packing can hug the body without invading
        the keep-out. Falls back to the plain bbox when the outline is
        missing, open, or not usefully decomposable.
        """
        segments: List[Tuple[float, float, float, float]] = []
        for child in self.node[1:]:
            if not isinstance(child, list):
                continue
            head = list_head(child)
            if head not in ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly", "fp_curve"):
                continue
            if _node_layer(child) not in ("F.CrtYd", "B.CrtYd"):
                continue
            if head == "fp_line":
                start = find_child(child, "start")
                end = find_child(child, "end")
                if start is not None and end is not None:
                    s = _floats(start)
                    e = _floats(end)
                    if len(s) >= 2 and len(e) >= 2:
                        segments.append((s[0], s[1], e[0], e[1]))
            elif head == "fp_poly":
                pts_node = find_child(child, "pts")
                pts = []
                if pts_node is not None:
                    for xy in child_lists(pts_node, "xy"):
                        vals = _floats(xy)
                        if len(vals) >= 2:
                            pts.append((vals[0], vals[1]))
                for i in range(len(pts)):
                    a, b = pts[i], pts[(i + 1) % len(pts)]
                    segments.append((a[0], a[1], b[0], b[1]))
            else:
                # rect / circle / arc: use the primitive's bbox outline.
                pts = _graphic_points(child)
                if pts:
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                    segments.extend([
                        (x0, y0, x1, y0), (x1, y0, x1, y1),
                        (x1, y1, x0, y1), (x0, y1, x0, y0),
                    ])
        if not segments:
            return [self.local_bbox()]
        xs = sorted({round(v, 4) for s in segments for v in (s[0], s[2])})
        ys = sorted({round(v, 4) for s in segments for v in (s[1], s[3])})
        if len(xs) < 2 or len(ys) < 2 or (len(xs) - 1) * (len(ys) - 1) > 400:
            return [self.local_bbox()]
        rects: List[Tuple[float, float, float, float]] = []
        for i in range(len(xs) - 1):
            for j in range(len(ys) - 1):
                cx = (xs[i] + xs[i + 1]) / 2
                cy = (ys[j] + ys[j + 1]) / 2
                if _point_in_outline(cx, cy, segments):
                    rects.append((xs[i], ys[j], xs[i + 1], ys[j + 1]))
        return rects if rects else [self.local_bbox()]

    def rects_at(self, rot: float) -> List[Tuple[float, float, float, float]]:
        """courtyard_rects() after rotating the footprint (exact for 90-degree
        steps, conservative AABBs otherwise)."""
        out = []
        for x0, y0, x1, y1 in self.courtyard_rects():
            corners = [rotate_local(x, y, rot) for x in (x0, x1) for y in (y0, y1)]
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            out.append((min(xs), min(ys), max(xs), max(ys)))
        return out


def _iter_xy(node: List[SExp]) -> Iterator[List[SExp]]:
    """Every (xy x y) node anywhere beneath `node`."""
    for child in node[1:]:
        if not isinstance(child, list):
            continue
        if list_head(child) == "xy":
            yield child
        else:
            yield from _iter_xy(child)


def _point_in_outline(px: float, py: float, segments: List[Tuple[float, float, float, float]]) -> bool:
    """Ray-casting point-in-polygon over a bag of outline segments."""
    py += 1e-4  # dodge vertices that sit exactly on the ray
    crossings = 0
    for x0, y0, x1, y1 in segments:
        if (y0 > py) == (y1 > py):
            continue
        x_at = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
        if x_at > px:
            crossings += 1
    return crossings % 2 == 1


def _rotate_child_at(child: List[SExp], delta: float) -> None:
    at_node = find_child(child, "at")
    if at_node is None:
        return
    vals = _floats(at_node)
    if len(vals) < 2:
        return
    old_angle = vals[2] if len(vals) >= 3 else 0.0
    new_angle = (old_angle + delta) % 360.0
    new_at: List[SExp] = [atom("at"), atom(_fmt_num(vals[0])), atom(_fmt_num(vals[1]))]
    if new_angle != 0.0:
        new_at.append(atom(_fmt_num(new_angle)))
    at_node[:] = new_at
