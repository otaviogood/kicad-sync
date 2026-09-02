from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from kicadsync import pcb_place
from kicadsync.kicad_pcb_model import KicadPcb
from kicadsync.kicad_sch_model import find_child
from kicadsync.kicad_sch_sexp import as_text, atom, list_head, qstr


DESIGN_JSON = """{
  // board-wide banner comment (not a separator)
  "root_components": [

    // ------------------------------------------------------------ regulator
    {
      "refdes": "U_REG",
      "mpn": "AP2112K",
      "footprint": "Package_TO_SOT_SMD:SOT-23-5",
      "description": "3.3V LDO",
      "notes": "",
      "connections": [
        { "number": "1", "name": "VIN", "net": "VIN" },
        { "number": "2", "name": "GND", "net": "GND" },
        { "number": "3", "name": "EN", "net": "VIN" },
        { "number": "5", "name": "VOUT", "net": "VOUT" }
      ]
    },
    {
      "refdes": "C_IN",
      "mpn": "CL10B104KB8NNNC",
      "footprint": "Capacitor_SMD:C_0603_1608Metric",
      "description": "100nF input cap",
      "notes": "",
      "connections": [
        { "number": "1", "name": "1", "net": "VIN" },
        { "number": "2", "name": "2", "net": "GND" }
      ]
    },
    {
      "refdes": "C_OUT",
      "mpn": "CL10B104KB8NNNC",
      "footprint": "Capacitor_SMD:C_0603_1608Metric",
      "description": "100nF output cap",
      "notes": "",
      "connections": [
        { "number": "1", "name": "1", "net": "VOUT" },
        { "number": "2", "name": "2", "net": "GND" }
      ]
    },

    // ------------------------------------------------------ output connector
    {
      "refdes": "J_OUT",
      "mpn": "B2B-PH-K-S",
      "footprint": "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
      "description": "output header",
      "notes": "",
      "connections": [
        { "number": "1", "name": "V", "net": "VOUT" },
        { "number": "2", "name": "G", "net": "GND" }
      ]
    }
  ],
  "sheet_definitions": [],
  "sheet_instances": []
}
"""


def _passive(ref: str, json_refdes: str, uuid: str, net1: str, net1_num: int,
             net2: str, net2_num: int, x: float, y: float, locked: bool = False) -> str:
    lock = "\n    (locked yes)" if locked else ""
    return f"""  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu"){lock}
    (uuid "{uuid}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "{uuid}-r") (effects (font (size 1 1))))
    (property "JsonRefdes" "{json_refdes}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{uuid}-j") (effects (font (size 1 1))))
    (fp_rect (start -1.48 -0.73) (end 1.48 0.73) (layer "F.CrtYd") (stroke (width 0.05) (type solid)) (fill no) (uuid "{uuid}-c"))
    (pad "1" smd roundrect (at -0.7875 0) (size 0.875 0.95) (layers "F.Cu") (net {net1_num} "{net1}") (uuid "{uuid}-p1"))
    (pad "2" smd roundrect (at 0.7875 0) (size 0.875 0.95) (layers "F.Cu") (net {net2_num} "{net2}") (uuid "{uuid}-p2"))
  )"""


# A footprint with an embedded keep-out zone. KiCad stores such a zone's
# polygon in ABSOLUTE board coordinates, so moving the footprint must move
# the polygon too (the ESP32-S3-WROOM-1 antenna keep-out is the real case).
ZONE_FP = """  (footprint "RF_Module:ZONE_PART"
    (layer "F.Cu")
    (uuid "z-fp")
    (at 100 100)
    (property "Reference" "U9" (at 0 -2 0) (layer "F.SilkS") (uuid "z-r") (effects (font (size 1 1))))
    (property "JsonRefdes" "U_ZONE" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "z-j") (effects (font (size 1 1))))
    (fp_rect (start -4 -8) (end 4 2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)) (fill no) (uuid "z-c"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VIN") (uuid "z-p1"))
    (zone
      (layers "F.Cu" "B.Cu")
      (uuid "z-zone")
      (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
               (copperpour not_allowed) (footprints not_allowed))
      (polygon (pts (xy 96 96) (xy 104 96) (xy 104 92) (xy 96 92)))
    )
  )"""


BOARD = f"""(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (net 0 "")
  (net 1 "VIN")
  (net 2 "GND")
  (net 3 "VOUT")
  (mystery_block (foo 1) (bar "keep me"))
  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (uuid "u-reg")
    (at 120 80)
    (property "Reference" "U1" (at 0 -2.5 0) (layer "F.SilkS") (uuid "u-reg-r") (effects (font (size 1 1))))
    (property "JsonRefdes" "U_REG" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "u-reg-j") (effects (font (size 1 1))))
    (fp_rect (start -1.92 -1.75) (end 1.92 1.75) (layer "F.CrtYd") (stroke (width 0.05) (type solid)) (fill no) (uuid "u-reg-c"))
    (pad "1" smd rect (at -1.3 -0.95) (size 1.06 0.65) (layers "F.Cu") (net 1 "VIN") (uuid "u-reg-p1"))
    (pad "2" smd rect (at -1.3 0.95) (size 1.06 0.65) (layers "F.Cu") (net 2 "GND") (uuid "u-reg-p2"))
    (pad "3" smd rect (at -1.3 0) (size 1.06 0.65) (layers "F.Cu") (net 1 "VIN") (uuid "u-reg-p3"))
    (pad "5" smd rect (at 1.3 -0.95) (size 1.06 0.65) (layers "F.Cu") (net 3 "VOUT") (uuid "u-reg-p5"))
  )
{_passive("C1", "C_IN", "c-in", "VIN", 1, "GND", 2, 90, 60)}
{_passive("C2", "C_OUT", "c-out", "VOUT", 3, "GND", 2, 95, 60)}
  (footprint "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical"
    (layer "F.Cu")
    (locked yes)
    (uuid "j-out")
    (at 150 110)
    (property "Reference" "J1" (at 0 -3 0) (layer "F.SilkS") (uuid "j-out-r") (effects (font (size 1 1))))
    (property "JsonRefdes" "J_OUT" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "j-out-j") (effects (font (size 1 1))))
    (fp_rect (start -3.45 -2.2) (end 5.45 3.2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)) (fill no) (uuid "j-out-c"))
    (pad "1" thru_hole roundrect (at 0 0) (size 1.2 1.7) (drill 0.75) (layers "*.Cu") (net 3 "VOUT") (uuid "j-out-p1"))
    (pad "2" thru_hole oval (at 2 0) (size 1.2 1.7) (drill 0.75) (layers "*.Cu") (net 2 "GND") (uuid "j-out-p2"))
  )
)
"""


def _abs_pad_positions(fp) -> dict:
    x, y, rot = fp.at
    out = {}
    for pad in fp.pads():
        from kicadsync.kicad_pcb_model import rotate_local
        dx, dy = rotate_local(pad.x, pad.y, rot)
        out[pad.number] = (x + dx, y + dy)
    return out


class CommentGroupTests(unittest.TestCase):
    def test_extract_comment_groups(self) -> None:
        groups = pcb_place.extract_comment_groups(DESIGN_JSON)
        self.assertEqual(groups["U_REG"], "regulator")
        self.assertEqual(groups["C_IN"], "regulator")
        self.assertEqual(groups["C_OUT"], "regulator")
        self.assertEqual(groups["J_OUT"], "output connector")

    def test_extraction_stops_at_sheet_definitions(self) -> None:
        text = DESIGN_JSON.replace(
            '"sheet_definitions": [],',
            '"sheet_definitions": [ { "sheet_id": "s", "file": "s.kicad_sch",'
            ' "components": [ { "refdes": "R_SHEET", "connections": [] } ] } ],',
        )
        groups = pcb_place.extract_comment_groups(text)
        self.assertNotIn("R_SHEET", groups)


class PcbModelTests(unittest.TestCase):
    def test_roundtrip_preserves_unknown_block(self) -> None:
        pcb = KicadPcb.from_text(BOARD)
        again = KicadPcb.from_text(pcb.to_text())
        unknown = [n for n in again.top_level_nodes() if list_head(n) == "mystery_block"]
        self.assertEqual(len(unknown), 1)

    def test_set_at_rotation_updates_pad_angles(self) -> None:
        pcb = KicadPcb.from_text(BOARD)
        fp = next(f for f in pcb.footprints() if f.reference == "C1")
        fp.set_at(10, 20, 90)
        again = KicadPcb.from_text(pcb.to_text())
        fp2 = next(f for f in again.footprints() if f.reference == "C1")
        self.assertEqual(fp2.at, (10.0, 20.0, 90.0))
        # Pad local coords unchanged; stored pad angle picks up the delta.
        pad_node = [n for n in fp2.node if isinstance(n, list) and list_head(n) == "pad"][0]
        from kicadsync.kicad_sch_model import find_child
        from kicadsync.kicad_sch_sexp import as_text
        at_node = find_child(pad_node, "at")
        vals = [as_text(v) for v in at_node[1:]]
        self.assertEqual(vals, ["-0.7875", "0", "90"])

    def test_locked_detection(self) -> None:
        pcb = KicadPcb.from_text(BOARD)
        locked = {f.reference: f.locked for f in pcb.footprints()}
        self.assertTrue(locked["J1"])
        self.assertFalse(locked["U1"])


ZONE_JSON_ENTRY = """    {
      "refdes": "U_ZONE",
      "mpn": "ZONE_PART",
      "footprint": "RF_Module:ZONE_PART",
      "description": "part with an antenna keep-out",
      "notes": "",
      "connections": [ { "number": "1", "name": "1", "net": "VIN" } ]
    },

    // ------------------------------------------------------ output connector"""


class FootprintZoneTests(unittest.TestCase):
    """An embedded keep-out zone must travel with its footprint."""

    ZONE_LOCAL = [(-4, -4), (4, -4), (4, -8), (-4, -8)]

    def _board_text(self) -> str:
        return BOARD.rstrip()[: -len(")")] + ZONE_FP + "\n)\n"

    def _zone_pts(self, fp):
        from kicadsync.kicad_pcb_model import _floats, _iter_xy
        zone = [n for n in fp.node if isinstance(n, list) and list_head(n) == "zone"][0]
        return [tuple(round(v, 3) for v in _floats(xy)) for xy in _iter_xy(zone)]

    def _expected(self, x, y, rot):
        from kicadsync.kicad_pcb_model import rotate_local
        return [
            tuple(round(o + v, 3) for o, v in zip((x, y), rotate_local(px, py, rot)))
            for px, py in self.ZONE_LOCAL
        ]

    def test_translation_moves_the_zone(self) -> None:
        pcb = KicadPcb.from_text(self._board_text())
        fp = next(f for f in pcb.footprints() if f.reference == "U9")
        self.assertEqual(self._zone_pts(fp), self._expected(100, 100, 0))
        fp.set_at(150, 120, 0)
        self.assertEqual(self._zone_pts(fp), self._expected(150, 120, 0))

    def test_rotation_moves_the_zone(self) -> None:
        pcb = KicadPcb.from_text(self._board_text())
        fp = next(f for f in pcb.footprints() if f.reference == "U9")
        fp.set_at(100, 100, 90)
        self.assertEqual(self._zone_pts(fp), self._expected(100, 100, 90))

    def test_zone_survives_a_full_place_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "demo.json"
            pcb_path = tmp / "demo.kicad_pcb"
            marker = "    // ------------------------------------------------------ output connector"
            self.assertIn(marker, DESIGN_JSON)
            json_path.write_text(DESIGN_JSON.replace(marker, ZONE_JSON_ENTRY), encoding="utf-8")
            pcb_path.write_text(self._board_text(), encoding="utf-8")

            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)

            fp = next(f for f in KicadPcb.from_file(pcb_path).footprints()
                      if f.reference == "U9")
            x, y, rot = fp.at
            self.assertNotEqual((x, y), (100.0, 100.0), "footprint should have moved")
            self.assertEqual(self._zone_pts(fp), self._expected(x, y, rot))


    def test_detached_zone_is_flagged_and_attached_zone_is_not(self) -> None:
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "demo.json"
            pcb_path = tmp / "demo.kicad_pcb"
            marker = "    // ------------------------------------------------------ output connector"
            json_path.write_text(DESIGN_JSON.replace(marker, ZONE_JSON_ENTRY), encoding="utf-8")

            # Zone polygon nowhere near the footprint at (100,100): stranded.
            stranded = ZONE_FP.replace(
                "(polygon (pts (xy 96 96) (xy 104 96) (xy 104 92) (xy 96 92)))",
                "(polygon (pts (xy 10 10) (xy 18 10) (xy 18 6) (xy 10 6)))",
            )
            pcb_path.write_text(BOARD.rstrip()[: -len(")")] + stranded + "\n)\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            self.assertIn("detached", out.getvalue())
            self.assertIn("U9", out.getvalue())

            # Consistent zone: no warning.
            pcb_path.write_text(self._board_text(), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            self.assertNotIn("detached", out.getvalue())


class PlacementTests(unittest.TestCase):
    def _run(self, tmp: Path) -> KicadPcb:
        json_path = tmp / "demo.json"
        pcb_path = tmp / "demo.kicad_pcb"
        json_path.write_text(DESIGN_JSON, encoding="utf-8")
        pcb_path.write_text(BOARD, encoding="utf-8")
        pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
        return KicadPcb.from_file(pcb_path)

    def test_place_synthetic_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pcb = self._run(Path(tmpdir))
            fps = {f.reference: f for f in pcb.footprints()}

            # Locked connector must not move.
            self.assertEqual(fps["J1"].at[:2], (150.0, 110.0))

            # C_IN's VIN pad ends up near U_REG's VIN pad (pad 1).
            u_pads = _abs_pad_positions(fps["U1"])
            c_in_pads = _abs_pad_positions(fps["C1"])
            dist = math.hypot(u_pads["1"][0] - c_in_pads["1"][0], u_pads["1"][1] - c_in_pads["1"][1])
            self.assertLess(dist, 4.0, f"C_IN VIN pad {dist:.2f}mm from U_REG VIN pad")

            # C_OUT near the VOUT pad (pad 5).
            c_out_pads = _abs_pad_positions(fps["C2"])
            dist = math.hypot(u_pads["5"][0] - c_out_pads["1"][0], u_pads["5"][1] - c_out_pads["1"][1])
            self.assertLess(dist, 4.0, f"C_OUT VOUT pad {dist:.2f}mm from U_REG VOUT pad")

            # No courtyard overlaps anywhere.
            boxes = []
            for ref, fp in fps.items():
                x, y, rot = fp.at
                b = fp.bbox_at(rot)
                boxes.append((ref, (b[0] + x, b[1] + y, b[2] + x, b[3] + y)))
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    ri, bi = boxes[i]
                    rj, bj = boxes[j]
                    overlap = not (
                        bi[2] <= bj[0] or bj[2] <= bi[0] or bi[3] <= bj[1] or bj[3] <= bi[1]
                    )
                    self.assertFalse(overlap, f"courtyards of {ri} and {rj} overlap: {bi} vs {bj}")


class GroupAnnotationTests(unittest.TestCase):
    def _prep(self, tmp: Path):
        json_path = tmp / "demo.json"
        pcb_path = tmp / "demo.kicad_pcb"
        json_path.write_text(DESIGN_JSON, encoding="utf-8")
        pcb_path.write_text(BOARD, encoding="utf-8")
        return json_path, pcb_path

    def _annotations(self, pcb: KicadPcb):
        rects, texts = [], []
        for node in pcb.top_level_nodes():
            if not pcb_place._is_annotation(node):
                continue
            (rects if list_head(node) == "gr_rect" else texts).append(node)
        return rects, texts

    def test_draws_labelled_box_per_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path, pcb_path = self._prep(Path(tmpdir))
            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            pcb = KicadPcb.from_file(pcb_path)
            rects, texts = self._annotations(pcb)
            self.assertEqual(len(rects), 2)
            self.assertEqual(len(texts), 2)

            labels = sorted(as_text(t[1]) for t in texts)
            self.assertEqual(labels, ["output connector  (1)", "regulator  (3)"])
            for node in rects + texts:
                self.assertEqual(find_child(node, "layer")[1].value, pcb_place.ANNOTATION_LAYER)

            # The documentation layer must be declared for KiCad to open it.
            layers = pcb.top_level_nodes("layers")[0]
            declared = {as_text(e[1]) for e in layers[1:] if isinstance(e, list) and len(e) >= 2}
            self.assertIn(pcb_place.ANNOTATION_LAYER, declared)

    def test_box_contains_its_group_and_label_sits_in_the_strip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path, pcb_path = self._prep(Path(tmpdir))
            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            pcb = KicadPcb.from_file(pcb_path)
            parts, _, report = pcb_place.build_parts(pcb, json_path, None)

            for group, refs in report.groups.items():
                uid = pcb_place._annotation_uuid("rect", group)
                rect = next(
                    n for n in pcb.top_level_nodes("gr_rect")
                    if as_text(find_child(n, "uuid")[1]) == uid
                )
                start = [float(as_text(v)) for v in find_child(rect, "start")[1:]]
                end = [float(as_text(v)) for v in find_child(rect, "end")[1:]]
                box = (start[0], start[1], end[0], end[1])

                for part in (p for p in parts if p.group == group):
                    x, y, rot = part.fp.at
                    b = part.fp.bbox_at(rot)
                    self.assertGreaterEqual(b[0] + x, box[0] - 1e-6, f"{part.ref} left of box")
                    self.assertGreaterEqual(b[1] + y, box[1] - 1e-6, f"{part.ref} above box")
                    self.assertLessEqual(b[2] + x, box[2] + 1e-6, f"{part.ref} right of box")
                    self.assertLessEqual(b[3] + y, box[3] + 1e-6, f"{part.ref} below box")

                text = next(
                    n for n in pcb.top_level_nodes("gr_text")
                    if as_text(find_child(n, "uuid")[1]) == pcb_place._annotation_uuid("text", group)
                )
                at = [float(as_text(v)) for v in find_child(text, "at")[1:3]]
                self.assertTrue(box[0] <= at[0] <= box[2])
                self.assertTrue(box[1] <= at[1] <= box[1] + pcb_place.LABEL_STRIP)

    def test_rerun_replaces_and_no_outlines_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path, pcb_path = self._prep(Path(tmpdir))
            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            first = self._annotations(KicadPcb.from_file(pcb_path))
            self.assertEqual((len(first[0]), len(first[1])), (2, 2))

            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            again = self._annotations(KicadPcb.from_file(pcb_path))
            self.assertEqual((len(again[0]), len(again[1])), (2, 2), "annotations accumulated")

            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None,
                                outlines=False)
            cleared = self._annotations(KicadPcb.from_file(pcb_path))
            self.assertEqual((len(cleared[0]), len(cleared[1])), (0, 0))

    def test_user_drawings_on_the_layer_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path, pcb_path = self._prep(Path(tmpdir))
            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)

            # A note the user drew on the same documentation layer.
            pcb = KicadPcb.from_file(pcb_path)
            pcb.append_top_level([
                atom("gr_text"), qstr("keep me"),
                [atom("at"), atom("10"), atom("10"), atom("0")],
                [atom("layer"), qstr(pcb_place.ANNOTATION_LAYER)],
                [atom("uuid"), qstr("11111111-2222-4333-8444-555555555555")],
            ])
            pcb.to_file(pcb_path)

            pcb_place.run_place(pcb_path=pcb_path, json_path=json_path, sch_path=None)
            after = KicadPcb.from_file(pcb_path)
            survivors = [
                as_text(n[1]) for n in after.top_level_nodes("gr_text")
                if not pcb_place._is_annotation(n)
            ]
            self.assertEqual(survivors, ["keep me"])
            self.assertEqual(len(self._annotations(after)[1]), 2)


if __name__ == "__main__":
    unittest.main()
