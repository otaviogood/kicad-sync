from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import os
import sys

# Keep the suite hermetic: never let a real ~/.cache/kicadsync (libs.json,
# JLC catalog) leak into generation results. Must be set before kicadsync
# imports because module-level defaults read it, and it is inherited by the
# subprocess invocations below.
os.environ["KICADSYNC_CACHE_DIR"] = tempfile.mkdtemp(prefix="kicadsync-test-cache-")

from kicadsync import json_schem_sync as sync
from kicadsync.jlc_price_report import collect_design_groups
from kicadsync.kicad_sch_model import KicadSchematic, find_child, get_property_value
from kicadsync.kicad_sch_sexp import as_text, atom, list_head, qstr
from kicadsync.kicad_sch_ops import candidate_label_points_for_pin, find_symbol_pin, set_symbol_property


REPO_ROOT = Path(__file__).resolve().parent
FIXTURES = REPO_ROOT / "fixtures"


def _kicad_cli_path() -> str | None:
    explicit = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if explicit.exists():
        return str(explicit)
    return shutil.which("kicad-cli")


class NativeBackendTests(unittest.TestCase):
    def assert_on_connection_grid(self, value: float, grid: float = 1.27) -> None:
        steps = round(value / grid)
        self.assertAlmostEqual(value, steps * grid, places=2)

    def test_roundtrip_preserves_unknown_top_level_block(self) -> None:
        text = """(kicad_sch
  (version 20231120)
  (generator "test")
  (unknown_block (foo 1) (bar "x"))
)"""
        doc = KicadSchematic.from_text(text)
        out = doc.to_text()
        parsed = KicadSchematic.from_text(out)
        unknown = [n for n in parsed.top_level_nodes() if list_head(n) == "unknown_block"]
        self.assertEqual(len(unknown), 1)

    def test_generate_pull_push_hierarchy_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "hierarchy_smoke.json"
            root_sch = tmp / "hierarchy_root.kicad_sch"
            child_sch = tmp / "led_chain.kicad_sch"

            json_path.write_text((FIXTURES / "hierarchy_smoke.json").read_text(encoding="utf-8"), encoding="utf-8")
            root_sch.write_text((FIXTURES / "hierarchy_root.kicad_sch").read_text(encoding="utf-8"), encoding="utf-8")
            child_sch.write_text((FIXTURES / "led_chain.kicad_sch").read_text(encoding="utf-8"), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "pull", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            pulled = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("root_components", pulled)
            self.assertEqual(len(pulled["sheet_instances"]), 2)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            updated = KicadSchematic.from_file(root_sch)
            self.assertGreaterEqual(len(updated.top_level_nodes("symbol")), 1)
            self.assertGreaterEqual(len(updated.top_level_nodes("wire")), 1)

            generated_root = tmp / "generated_root.kicad_sch"
            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(generated_root)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            generated_doc = KicadSchematic.from_file(generated_root)
            self.assertEqual(len(generated_doc.top_level_nodes("sheet")), 2)
            self.assertEqual(len(generated_doc.top_level_nodes("sheet_instances")), 1)

    def test_generate_supports_no_connect_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "nc.json"
            out_sch = tmp / "nc.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "U_TEST",
                                "mpn": "TEST-MPN",
                                "LCSC": "C123456",
                                "description": "Symbol with an explicit NC pin.",
                                "notes": "Exercises no_connect generation.",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "SIG", "scope": "global"},
                                    {"number": "2", "name": "NC", "net": "__NC__"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            self.assertEqual(len(generated_doc.top_level_nodes("no_connect")), 1)
            symbol = next(sym for sym in generated_doc.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_TEST")
            self.assertEqual(get_property_value(symbol, "LCSC"), "C123456")

    def test_push_bootstraps_missing_root_schematic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "bootstrap_missing.json"
            root_sch = tmp / "bootstrap_missing.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_BOOT",
                        "mpn": "BOOTSTRAP",
                        "LCSC": "C424242",
                        "description": "Created by push on a missing schematic",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "IN", "net": "SIG_IN", "scope": "global"},
                            {"number": "2", "name": "OUT", "net": "SIG_OUT", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated = KicadSchematic.from_file(root_sch)
            symbol = next(sym for sym in generated.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_BOOT")
            self.assertEqual(get_property_value(symbol, "MPN"), "BOOTSTRAP")
            self.assertEqual(get_property_value(symbol, "LCSC"), "C424242")

    def test_push_bootstraps_blank_hierarchical_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "bootstrap_hierarchy.json"
            root_sch = tmp / "bootstrap_hierarchy.kicad_sch"
            child_sch = tmp / "bootstrap_child.kicad_sch"
            payload = {
                "root_components": [],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "bootstrap_child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILDPART",
                                "description": "Child symbol",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "IN", "scope": "hierarchical"},
                                    {"number": "2", "name": "OUT", "net": "OUT", "scope": "hierarchical"},
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_A",
                        "sheet_id": "child",
                        "pin_map": [
                            {"name": "IN", "net": "TOP_IN", "scope": "global"},
                            {"name": "OUT", "net": "TOP_OUT", "scope": "global"},
                        ],
                    }
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            root_sch.write_text(
                """(kicad_sch
  (version 20231120)
  (generator "test")
)""",
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed_root = KicadSchematic.from_file(root_sch)
            self.assertEqual(len(pushed_root.top_level_nodes("sheet")), 1)
            self.assertTrue(child_sch.exists())
            pushed_child = KicadSchematic.from_file(child_sch)
            self.assertTrue(any(get_property_value(sym, "JsonRefdes") == "U_CHILD" for sym in pushed_child.top_level_nodes("symbol")))

    def test_generate_uses_matching_symbol_from_libs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "matched.json"
            libs_path = tmp / "libs.json"
            out_sch = tmp / "matched.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "D_ESD_USB",
                                "mpn": "TPD2EUSB30DRTR",
                                "description": "USB D+/D- ESD protector placed at the connector side.",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "D+", "net": "USB_DP", "scope": "global"},
                                    {"number": "3", "name": "GND", "net": "GND", "scope": "global"},
                                    {"number": "2", "name": "D-", "net": "USB_DN", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            libs_path.write_text(
                """[list-libs] Scanning 1 library files
[
  {
    "lib_id": "Power_Protection:TPD2EUSB30",
    "library": "Power_Protection",
    "name": "TPD2EUSB30",
    "reference": "U",
    "description": "2-Channel ESD Protection for Super-Speed USB 3.0 Interface, DRT-3",
    "footprint": "Package_TO_SOT_SMD:Texas_DRT-3",
    "datasheet": "http://www.ti.com/lit/ds/symlink/tpd2eusb30a.pdf",
    "keywords": "ESD protection USB 3.0",
    "pins": [
      {"number": "1", "name": "D+", "type": "passive"},
      {"number": "3", "name": "GND", "type": "passive"},
      {"number": "2", "name": "D-", "type": "passive"}
    ]
  }
]
""",
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            text = out_sch.read_text(encoding="utf-8")
            self.assertIn('(lib_id "generated:TPD2EUSB30DRTR")', text)
            self.assertIn('"Footprint"', text)
            self.assertIn('"Package_TO_SOT_SMD:Texas_DRT-3"', text)
            self.assertNotIn('(lib_id "Power_Protection:TPD2EUSB30")', text)

    def test_generate_marks_unmatched_placeholder_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "unmatched.json"
            out_sch = tmp / "unmatched.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "U_TEST",
                                "mpn": "TOTALLY_UNKNOWN_PART",
                                "description": "Should remain a generated placeholder.",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "SIG_IN", "scope": "global"},
                                    {"number": "2", "name": "OUT", "net": "SIG_OUT", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            text = out_sch.read_text(encoding="utf-8")
            self.assertIn('(lib_id "generated:U_TEST")', text)
            self.assertIn('(width 0.508)', text)
            self.assertIn('"NOLIB"', text)

    def test_generate_test_point_without_mpn_uses_footprint_and_excludes_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "tp.json"
            out_sch = tmp / "tp.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "TP1",
                                "description": "Ground test point",
                                "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "1", "net": "GND", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            text = out_sch.read_text(encoding="utf-8")
            self.assertIn('(lib_id "generated:TP1")', text)
            self.assertIn('(in_bom no)', text)
            self.assertIn('(dnp no)', text)
            self.assertIn('"TestPoint:TestPoint_Pad_D1.5mm"', text)

    def test_generate_dnp_component_keeps_board_and_sets_kicad_dnp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "dnp.json"
            out_sch = tmp / "dnp.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "R_SNUB_A",
                                "description": "Optional snubber resistor",
                                "footprint": "Resistor_SMD:R_0603_1608Metric",
                                "dnp": True,
                                "notes": "Normally DNP. Populate only if bring-up shows ringing.",
                                "connections": [
                                    {"number": "1", "name": "1", "net": "OUT_A", "scope": "global"},
                                    {"number": "2", "name": "2", "net": "SNUB_A", "scope": "local"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            text = out_sch.read_text(encoding="utf-8")
            self.assertIn('(in_bom no)', text)
            self.assertIn('(on_board yes)', text)
            self.assertIn('(dnp yes)', text)

    def test_generate_legacy_dnp_notes_without_mpn_set_kicad_dnp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "legacy_dnp.json"
            out_sch = tmp / "legacy_dnp.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "C_SNUB_A",
                                "description": "Optional snubber capacitor",
                                "footprint": "Capacitor_SMD:C_0603_1608Metric",
                                "notes": "Normally DNP. Populate only if bring-up shows ringing.",
                                "connections": [
                                    {"number": "1", "name": "1", "net": "SNUB_A", "scope": "local"},
                                    {"number": "2", "name": "2", "net": "OUT_B", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            text = out_sch.read_text(encoding="utf-8")
            self.assertIn('(in_bom no)', text)
            self.assertIn('(on_board yes)', text)
            self.assertIn('(dnp yes)', text)

    def test_push_pull_preserve_test_point_footprint_without_mpn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "tp_push_pull.json"
            out_sch = tmp / "tp_push_pull.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "TP1",
                                "description": "Ground test point",
                                "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "1", "net": "GND", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "pull", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pulled = json.loads(json_path.read_text(encoding="utf-8"))
            comp = pulled["root_components"][0]
            self.assertEqual(comp["refdes"], "TP1")
            self.assertEqual(comp["footprint"], "TestPoint:TestPoint_Pad_D1.5mm")
            self.assertNotIn("mpn", comp)
            self.assertNotIn("dnp", comp)

    def test_push_pull_round_trips_dnp_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "dnp_roundtrip.json"
            out_sch = tmp / "dnp_roundtrip.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "R_SNUB_A",
                        "mpn": "RC0603FR-07100RL",
                        "LCSC": "C105588",
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
                        "description": "Optional snubber resistor",
                        "dnp": True,
                        "notes": "Normally DNP. Populate only if bring-up shows ringing.",
                        "connections": [
                            {"number": "1", "name": "1", "net": "OUT_A", "scope": "global"},
                            {"number": "2", "name": "2", "net": "SNUB_A", "scope": "local"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "pull", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pulled = json.loads(json_path.read_text(encoding="utf-8"))
            comp = pulled["root_components"][0]
            self.assertEqual(comp["mpn"], "RC0603FR-07100RL")
            self.assertEqual(comp["LCSC"], "C105588")
            self.assertTrue(comp["dnp"])

    def test_push_pull_round_trips_lcsc_property(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "lcsc_roundtrip.json"
            out_sch = tmp / "lcsc_roundtrip.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_SYNC",
                        "mpn": "SYNC-PART",
                        "LCSC": "C777777",
                        "description": "Round-trip LCSC sync test",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "IN", "net": "SIG_IN", "scope": "global"},
                            {"number": "2", "name": "OUT", "net": "SIG_OUT", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated = KicadSchematic.from_file(out_sch)
            symbol = next(sym for sym in generated.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_SYNC")
            self.assertEqual(get_property_value(symbol, "LCSC"), "C777777")

            set_symbol_property(symbol, "LCSC", "C888888")
            generated.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "pull", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pulled = json.loads(json_path.read_text(encoding="utf-8"))
            comp = pulled["root_components"][0]
            self.assertEqual(comp["LCSC"], "C888888")

    def test_jlc_report_skips_components_without_mpn(self) -> None:
        design = {
            "root_components": [
                {
                    "refdes": "TP1",
                    "description": "Ground test point",
                    "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                    "notes": "",
                    "connections": [{"number": "1", "name": "1", "net": "GND"}],
                },
                {
                    "refdes": "R1",
                    "mpn": "RC0603FR-0710KL",
                    "description": "10k resistor",
                    "notes": "",
                    "connections": [
                        {"number": "1", "name": "1", "net": "SIG"},
                        {"number": "2", "name": "2", "net": "GND"},
                    ],
                },
            ],
            "sheet_definitions": [],
            "sheet_instances": [],
        }
        groups = collect_design_groups(design)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].schematic_mpn, "RC0603FR-0710KL")

    def test_generate_places_labels_with_short_stubs_and_correct_side_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "three_pin.json"
            out_sch = tmp / "three_pin.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "D_ESD_USB",
                                "mpn": "TPD2EUSB30DRTR",
                                "description": "3-pin example.",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IO1", "net": "USB_DP_CONN", "scope": "local"},
                                    {"number": "3", "name": "IO2", "net": "USB_DN_CONN", "scope": "local"},
                                    {"number": "2", "name": "GND", "net": "GND", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            self.assertEqual(len(generated_doc.top_level_nodes("wire")), 3)
            for wire in generated_doc.top_level_nodes("wire"):
                pts = find_child(wire, "pts")
                self.assertIsNotNone(pts)
                xy_nodes = [node for node in pts[1:] if isinstance(node, list) and list_head(node) == "xy"]
                self.assertEqual(len(xy_nodes), 2)
                for xy in xy_nodes:
                    self.assert_on_connection_grid(float(as_text(xy[1])))
                    self.assert_on_connection_grid(float(as_text(xy[2])))

            labels = generated_doc.top_level_nodes("label") + generated_doc.top_level_nodes("global_label")
            self.assertEqual(len(labels), 3)

            by_net = {as_text(node[1]): node for node in labels}

            # Placeholder pins are numerically sorted: 1,2 on the left, 3 right.
            left_top_at = find_child(by_net["USB_DP_CONN"], "at")
            left_top_x = float(as_text(left_top_at[1]))
            left_top_y = float(as_text(left_top_at[2]))
            self.assertLess(left_top_x, 50.0)
            self.assertEqual(as_text(left_top_at[3]), "180")
            self.assert_on_connection_grid(left_top_x)
            self.assert_on_connection_grid(left_top_y)

            left_bottom_at = find_child(by_net["GND"], "at")
            left_bottom_x = float(as_text(left_bottom_at[1]))
            left_bottom_y = float(as_text(left_bottom_at[2]))
            self.assertLess(left_bottom_x, 50.0)
            self.assertEqual(as_text(left_bottom_at[3]), "180")
            self.assert_on_connection_grid(left_bottom_x)
            self.assert_on_connection_grid(left_bottom_y)

            right_top_at = find_child(by_net["USB_DN_CONN"], "at")
            right_top_x = float(as_text(right_top_at[1]))
            right_top_y = float(as_text(right_top_at[2]))
            self.assertGreater(right_top_x, 50.0)
            self.assertEqual(as_text(right_top_at[3]), "0")
            self.assert_on_connection_grid(right_top_x)
            self.assert_on_connection_grid(right_top_y)
            self.assertLess(left_top_y, left_bottom_y)
            self.assertAlmostEqual(left_top_y, right_top_y, places=2)

    def test_generate_three_pin_case_is_connected_in_kicad_cli(self) -> None:
        cli = _kicad_cli_path()
        if not cli:
            self.skipTest("kicad-cli not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "three_pin.json"
            out_sch = tmp / "three_pin.kicad_sch"
            out_net = tmp / "three_pin.net"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "D_ESD_USB",
                                "mpn": "TPD2EUSB30DRTR",
                                "description": "3-pin example.",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IO1", "net": "USB_DP_CONN", "scope": "local"},
                                    {"number": "3", "name": "IO2", "net": "USB_DN_CONN", "scope": "local"},
                                    {"number": "2", "name": "GND", "net": "GND", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            subprocess.run(
                [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out_net), str(out_sch)],
                check=True,
                cwd=str(tmp),
            )
            netlist = out_net.read_text(encoding="utf-8")
            self.assertIn('(name "/USB_DP_CONN")', netlist)
            self.assertIn('(name "/USB_DN_CONN")', netlist)
            self.assertIn('(name "GND")', netlist)
            self.assertNotIn('unconnected-(D_ESD_USB-IO1-Pad1)', netlist)
            self.assertNotIn('unconnected-(D_ESD_USB-IO2-Pad3)', netlist)
            self.assertNotIn('unconnected-(D_ESD_USB-GND-Pad2)', netlist)

    def test_push_preserves_generated_three_pin_label_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "three_pin.json"
            out_sch = tmp / "three_pin.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "D_ESD_USB",
                        "mpn": "TPD2EUSB30DRTR",
                        "description": "3-pin example.",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "IO1", "net": "USB_DP_CONN", "scope": "local"},
                            {"number": "3", "name": "IO2", "net": "USB_DN_CONN", "scope": "local"},
                            {"number": "2", "name": "GND", "net": "GND", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            before = sorted(
                (
                    list_head(node),
                    as_text(node[1]),
                    as_text(find_child(node, "at")[1]),
                    as_text(find_child(node, "at")[2]),
                    as_text(find_child(node, "at")[3]),
                )
                for node in generated_doc.top_level_nodes("label") + generated_doc.top_level_nodes("global_label")
            )
            self.assertEqual(len(before), 3)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed_doc = KicadSchematic.from_file(out_sch)
            after = sorted(
                (
                    list_head(node),
                    as_text(node[1]),
                    as_text(find_child(node, "at")[1]),
                    as_text(find_child(node, "at")[2]),
                    as_text(find_child(node, "at")[3]),
                )
                for node in pushed_doc.top_level_nodes("label") + pushed_doc.top_level_nodes("global_label")
            )
            self.assertEqual(after, before)

    def test_push_upgrades_nearby_local_label_to_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "tp.json"
            out_sch = tmp / "tp.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "TP_USB",
                        "description": "USB probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "USB_SIG", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            global_label = next(node for node in doc.top_level_nodes("global_label") if as_text(node[1]) == "USB_SIG")
            global_label[0] = atom("label")
            global_label[:] = [
                child
                for child in global_label
                if not (
                    isinstance(child, list)
                    and list_head(child) in {"shape", "fields_autoplaced"}
                    or (
                        isinstance(child, list)
                        and list_head(child) == "property"
                        and len(child) >= 2
                        and as_text(child[1]) == "Intersheetrefs"
                    )
                )
            ]
            doc.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            self.assertFalse(
                any(as_text(node[1]) == "USB_SIG" for node in pushed.top_level_nodes("label")),
            )
            self.assertTrue(
                any(as_text(node[1]) == "USB_SIG" for node in pushed.top_level_nodes("global_label")),
            )

    def test_push_replaces_label_when_net_name_and_scope_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "clk.json"
            out_sch = tmp / "clk.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "TP_CLK",
                        "description": "Clock probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "XIN", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            payload["root_components"][0]["connections"][0]["net"] = "XIN_XTAL"
            payload["root_components"][0]["connections"][0]["scope"] = "local"
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            labels = [node for node in pushed.top_level_nodes("label") if as_text(node[1]) == "XIN_XTAL"]
            self.assertEqual(len(labels), 1)
            label_at = find_child(labels[0], "at")
            self.assertIsNotNone(label_at)
            x = float(as_text(label_at[1]))
            y = float(as_text(label_at[2]))

            self.assertEqual(
                sum(
                    1
                    for node in pushed.top_level_nodes("global_label")
                    if abs(float(as_text(find_child(node, "at")[1])) - x) < 0.02
                    and abs(float(as_text(find_child(node, "at")[2])) - y) < 0.02
                ),
                0,
            )
            self.assertFalse(any(as_text(node[1]) == "XIN" for node in pushed.top_level_nodes("global_label")))

    def test_push_removes_stale_label_when_symbol_rehomes_and_scope_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "rehome_scope.json"
            libs_path = tmp / "libs.json"
            out_sch = tmp / "rehome_scope.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "J_CLK",
                        "mpn": "TEST-HEADER-3",
                        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
                        "description": "Three-pin header",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "XTAL", "net": "XTAL_SIG", "scope": "global"},
                            {"number": "2", "name": "MCU", "net": "MCU_CLK", "scope": "global"},
                            {"number": "3", "name": "EXT", "net": "EXT_CLK", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            before = KicadSchematic.from_file(out_sch)
            before_symbol = next(sym for sym in before.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_CLK")
            before_pin1 = find_symbol_pin(before_symbol, "1", "", before)
            before_label_x, before_label_y = candidate_label_points_for_pin(before_pin1.x, before_pin1.y, before_pin1.angle, 2.54)[0]

            libs_path.write_text(
                """[list-libs] Scanning 1 library files
[
  {
    "lib_id": "Connector:Conn_01x03_Test",
    "library": "Connector",
    "name": "TEST-HEADER-3",
    "reference": "J",
    "description": "Reordered three-pin header for sync regression coverage.",
    "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "datasheet": "~",
    "pins": [
      {"number": "2", "name": "MCU", "type": "passive"},
      {"number": "3", "name": "EXT", "type": "passive"},
      {"number": "1", "name": "XTAL", "type": "passive"}
    ]
  }
]
""",
                encoding="utf-8",
            )
            payload["root_components"][0]["connections"][0]["scope"] = "local"
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_CLK")
            pushed_pin1 = find_symbol_pin(pushed_symbol, "1", "", pushed)
            pushed_label_x, pushed_label_y = candidate_label_points_for_pin(pushed_pin1.x, pushed_pin1.y, pushed_pin1.angle, 2.54)[0]

            self.assertNotEqual(
                (round(before_label_x, 2), round(before_label_y, 2)),
                (round(pushed_label_x, 2), round(pushed_label_y, 2)),
            )
            self.assertEqual(
                sum(
                    1
                    for node in pushed.top_level_nodes("label")
                    if as_text(node[1]) == "XTAL_SIG"
                    and abs(float(as_text(find_child(node, "at")[1])) - pushed_label_x) < 0.02
                    and abs(float(as_text(find_child(node, "at")[2])) - pushed_label_y) < 0.02
                ),
                1,
            )
            self.assertEqual(
                sum(1 for node in pushed.top_level_nodes("global_label") if as_text(node[1]) == "XTAL_SIG"),
                0,
            )
            self.assertEqual(
                sum(
                    1
                    for head in ("label", "global_label")
                    for node in pushed.top_level_nodes(head)
                    if as_text(node[1]) == "XTAL_SIG"
                    and abs(float(as_text(find_child(node, "at")[1])) - before_label_x) < 0.02
                    and abs(float(as_text(find_child(node, "at")[2])) - before_label_y) < 0.02
                ),
                0,
            )

    def test_push_rehomes_standard_library_symbol_to_generated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "matched.json"
            libs_path = tmp / "libs.json"
            out_sch = tmp / "matched.kicad_sch"
            json_path.write_text(
                json.dumps(
                    {
                        "root_components": [
                            {
                                "refdes": "D_ESD_USB",
                                "mpn": "TPD2EUSB30DRTR",
                                "description": "USB ESD diode.",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "D+", "net": "USB_DP", "scope": "global"},
                                    {"number": "3", "name": "GND", "net": "GND", "scope": "global"},
                                    {"number": "2", "name": "D-", "net": "USB_DN", "scope": "global"},
                                ],
                            }
                        ],
                        "sheet_definitions": [],
                        "sheet_instances": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            libs_path.write_text(
                """[list-libs] Scanning 1 library files
[
  {
    "lib_id": "Power_Protection:TPD2EUSB30",
    "library": "Power_Protection",
    "name": "TPD2EUSB30",
    "reference": "U",
    "description": "2-Channel ESD Protection for Super-Speed USB 3.0 Interface, DRT-3",
    "footprint": "Package_TO_SOT_SMD:Texas_DRT-3",
    "datasheet": "http://www.ti.com/lit/ds/symlink/tpd2eusb30a.pdf",
    "keywords": "ESD protection USB 3.0",
    "pins": [
      {"number": "1", "name": "D+", "type": "passive"},
      {"number": "3", "name": "GND", "type": "passive"},
      {"number": "2", "name": "D-", "type": "passive"}
    ]
  }
]
""",
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            lib_symbols = doc.find_first("lib_symbols")
            generated_entry = next(
                node for node in lib_symbols[1:] if isinstance(node, list) and list_head(node) == "symbol"
            )
            generated_entry[1] = qstr("Power_Protection:TPD2EUSB30")
            placed_symbol = doc.top_level_nodes("symbol")[0]
            lib_id = find_child(placed_symbol, "lib_id")
            lib_id[1] = qstr("Power_Protection:TPD2EUSB30")
            doc.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = pushed.top_level_nodes("symbol")[0]
            self.assertEqual(get_property_value(pushed_symbol, "MPN"), "TPD2EUSB30DRTR")
            self.assertEqual(as_text(find_child(pushed_symbol, "lib_id")[1]), "generated:TPD2EUSB30DRTR")
            pushed_lib_symbols = pushed.find_first("lib_symbols")
            self.assertTrue(
                any(
                    isinstance(node, list)
                    and list_head(node) == "symbol"
                    and len(node) >= 2
                    and as_text(node[1]) == "generated:TPD2EUSB30DRTR"
                    for node in pushed_lib_symbols[1:]
                )
            )

    def test_push_updates_value_and_clears_stale_instance_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "stale_props.json"
            out_sch = tmp / "stale_props.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_SYNC",
                        "mpn": "OLD-MPN",
                        "LCSC": "C111111",
                        "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
                        "description": "Original description",
                        "notes": "Original notes",
                        "connections": [
                            {"number": "1", "name": "IN", "net": "SIG_IN"},
                            {"number": "2", "name": "OUT", "net": "SIG_OUT"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            payload["root_components"][0]["mpn"] = "NEW-MPN"
            payload["root_components"][0]["LCSC"] = "C222222"
            payload["root_components"][0]["footprint"] = ""
            payload["root_components"][0]["description"] = ""
            payload["root_components"][0]["notes"] = ""
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_SYNC")
            self.assertEqual(get_property_value(pushed_symbol, "MPN"), "NEW-MPN")
            self.assertEqual(get_property_value(pushed_symbol, "LCSC"), "C222222")
            self.assertEqual(get_property_value(pushed_symbol, "Value"), "NEW-MPN")
            self.assertEqual(get_property_value(pushed_symbol, "Footprint"), "")
            self.assertEqual(get_property_value(pushed_symbol, "Description"), "")
            self.assertEqual(get_property_value(pushed_symbol, "Notes"), "")
            self.assertEqual(as_text(find_child(pushed_symbol, "in_bom")[1]), "yes")

            payload["root_components"][0]["mpn"] = ""
            payload["root_components"][0]["LCSC"] = ""
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_SYNC")
            self.assertEqual(get_property_value(pushed_symbol, "MPN"), "")
            self.assertEqual(get_property_value(pushed_symbol, "LCSC"), "")
            self.assertEqual(get_property_value(pushed_symbol, "Value"), "U_SYNC")
            self.assertEqual(as_text(find_child(pushed_symbol, "in_bom")[1]), "no")

    def test_push_restores_library_backed_instance_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "library_backed_props.json"
            libs_path = tmp / "libs.json"
            out_sch = tmp / "library_backed_props.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_FLASH",
                        "mpn": "W25Q128JVSIQ",
                        "description": "",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "CS#", "net": "CS"},
                            {"number": "2", "name": "DO/IO1", "net": "IO1"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            libs_path.write_text(
                """[list-libs] Scanning 1 library files
[
  {
    "lib_id": "Memory_Flash:W25Q128JVS",
    "library": "Memory_Flash",
    "name": "W25Q128JVS",
    "reference": "U",
    "description": "Library flash description",
    "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
    "datasheet": "https://example.invalid/w25q128.pdf",
    "pins": [
      {"number": "1", "name": "CS#", "type": "passive"},
      {"number": "2", "name": "DO/IO1", "type": "passive"}
    ]
  }
]
""",
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            symbol = next(sym for sym in doc.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_FLASH")
            set_symbol_property(symbol, "Value", "STALE-VALUE")
            set_symbol_property(symbol, "Footprint", "STALE-FOOTPRINT")
            set_symbol_property(symbol, "Datasheet", "STALE-DATASHEET")
            set_symbol_property(symbol, "Description", "STALE-DESCRIPTION")
            doc.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_FLASH")
            self.assertEqual(get_property_value(pushed_symbol, "Value"), "W25Q128JVSIQ")
            self.assertEqual(
                get_property_value(pushed_symbol, "Footprint"),
                "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
            )
            self.assertEqual(get_property_value(pushed_symbol, "Datasheet"), "https://example.invalid/w25q128.pdf")
            self.assertEqual(get_property_value(pushed_symbol, "Description"), "Library flash description")

    def test_push_refreshes_generated_symbol_when_component_gains_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "grow_symbol.json"
            out_sch = tmp / "grow_symbol.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "J_TEST",
                        "mpn": "TEST-CONN",
                        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
                        "description": "Test connector",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "P1", "net": "NET1"},
                            {"number": "2", "name": "P2", "net": "NET2"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            payload["root_components"][0]["connections"].append({"number": "3", "name": "NC", "net": "__NC__"})
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotIn("Could not find pin number='3'", result.stdout)

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_TEST")
            self.assertTrue(
                any(
                    isinstance(node, list)
                    and list_head(node) == "pin"
                    and len(node) >= 2
                    and as_text(node[1]) == "3"
                    for node in pushed_symbol[1:]
                )
            )
            self.assertEqual(len(pushed.top_level_nodes("no_connect")), 1)

            lib_symbols = pushed.find_first("lib_symbols")
            lib_entry = next(
                node
                for node in lib_symbols[1:]
                if isinstance(node, list) and list_head(node) == "symbol" and len(node) >= 2 and as_text(node[1]) == "generated:J_TEST"
            )
            pin_numbers = []
            stack = [lib_entry]
            while stack:
                node = stack.pop()
                for child in node[1:]:
                    if not isinstance(child, list):
                        continue
                    if list_head(child) == "pin":
                        number_node = find_child(child, "number")
                        if number_node is not None and len(number_node) >= 2:
                            pin_numbers.append(as_text(number_node[1]))
                    else:
                        stack.append(child)
            self.assertIn("3", pin_numbers)

    def test_push_uses_component_pins_when_library_match_has_no_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "pinless_match.json"
            libs_path = tmp / "libs.json"
            out_sch = tmp / "pinless_match.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_FLASH",
                        "mpn": "W25Q128JVSIQ",
                        "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
                        "description": "Flash",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "CS#", "net": "CS"},
                            {"number": "2", "name": "DO/IO1", "net": "IO1"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            libs_path.write_text(
                """[list-libs] Scanning 1 library files
[
  {
    "lib_id": "Memory_Flash:W25Q128JVS",
    "library": "Memory_Flash",
    "name": "W25Q128JVS",
    "reference": "U",
    "description": "Flash",
    "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
    "datasheet": "https://example.invalid",
    "pins": []
  }
]
""",
                encoding="utf-8",
            )

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            payload["root_components"][0]["connections"].append({"number": "3", "name": "IO2", "net": "__NC__"})
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotIn("Could not find pin number='1'", result.stdout)
            self.assertNotIn("Could not find pin number='2'", result.stdout)
            self.assertNotIn("Could not find pin number='3'", result.stdout)

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_FLASH")
            self.assertTrue(
                any(
                    isinstance(node, list)
                    and list_head(node) == "pin"
                    and len(node) >= 2
                    and as_text(node[1]) == "3"
                    for node in pushed_symbol[1:]
                )
            )
            self.assertEqual(len(pushed.top_level_nodes("no_connect")), 1)

    def test_generate_sorts_generated_symbol_pins_naturally(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "sorted_pins.json"
            out_sch = tmp / "sorted_pins.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_SORT",
                        "mpn": "SORTME",
                        "footprint": "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
                        "description": "Pin order test",
                        "notes": "",
                        "connections": [
                            {"number": "10", "name": "TEN", "net": "NET10"},
                            {"number": "2", "name": "TWO", "net": "NET2"},
                            {"number": "1", "name": "ONE", "net": "NET1"},
                            {"number": "A10", "name": "A_TEN", "net": "NETA10"},
                            {"number": "A2", "name": "A_TWO", "net": "NETA2"},
                            {"number": "PPAD", "name": "PPAD", "net": "GND"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated = KicadSchematic.from_file(out_sch)
            lib_symbols = generated.find_first("lib_symbols")
            lib_entry = next(
                node
                for node in lib_symbols[1:]
                if isinstance(node, list) and list_head(node) == "symbol" and len(node) >= 2 and as_text(node[1]) == "generated:U_SORT"
            )
            pin_numbers = []
            stack = [lib_entry]
            while stack:
                node = stack.pop()
                for child in node[1:]:
                    if not isinstance(child, list):
                        continue
                    if list_head(child) == "pin":
                        number_node = find_child(child, "number")
                        if number_node is not None and len(number_node) >= 2:
                            pin_numbers.append(as_text(number_node[1]))
                    else:
                        stack.append(child)
            self.assertEqual(pin_numbers, ["1", "2", "10", "A2", "A10", "PPAD"])

    def test_push_removes_stale_artifacts_when_component_gains_mid_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "regrow_regulator.json"
            out_sch = tmp / "regrow_regulator.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_REG",
                        "mpn": "TLV75533PDBVR",
                        "footprint": "Package_TO_SOT_SMD:SOT-23-5",
                        "description": "Regulator",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "IN", "net": "VIN_5V"},
                            {"number": "2", "name": "GND", "net": "GND"},
                            {"number": "3", "name": "EN", "net": "VIN_5V"},
                            {"number": "5", "name": "OUT", "net": "3V3"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            before = KicadSchematic.from_file(out_sch)
            before_symbol = next(sym for sym in before.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_REG")
            stale_points = []
            for pin_number in ("1", "2", "3", "5"):
                pin = find_symbol_pin(before_symbol, pin_number, "", before)
                stale_points.append((pin.x, pin.y))
                stale_points.extend(candidate_label_points_for_pin(pin.x, pin.y, pin.angle, 2.54))

            payload["root_components"][0]["connections"].insert(3, {"number": "4", "name": "NC", "net": "__NC__"})
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            pushed_symbol = next(sym for sym in pushed.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "U_REG")

            current_points = set()
            current_labels = {
                "1": "VIN_5V",
                "2": "GND",
                "3": "VIN_5V",
                "5": "3V3",
            }
            for pin_number, expected_net in current_labels.items():
                pin = find_symbol_pin(pushed_symbol, pin_number, "", pushed)
                current_points.add((round(pin.x, 2), round(pin.y, 2)))
                label_points = candidate_label_points_for_pin(pin.x, pin.y, pin.angle, 2.54)
                for lx, ly in label_points:
                    current_points.add((round(lx, 2), round(ly, 2)))
                label_x, label_y = label_points[0]
                self.assertEqual(
                    sum(
                        1
                        for node in pushed.top_level_nodes("global_label")
                        if as_text(node[1]) == expected_net
                        and abs(float(as_text(find_child(node, "at")[1])) - label_x) < 0.02
                        and abs(float(as_text(find_child(node, "at")[2])) - label_y) < 0.02
                    ),
                    1,
                )

            pin4 = find_symbol_pin(pushed_symbol, "4", "", pushed)
            self.assertEqual(
                sum(
                    1
                    for node in pushed.top_level_nodes("no_connect")
                    if abs(float(as_text(find_child(node, "at")[1])) - pin4.x) < 0.02
                    and abs(float(as_text(find_child(node, "at")[2])) - pin4.y) < 0.02
                ),
                1,
            )
            pin4_label_points = candidate_label_points_for_pin(pin4.x, pin4.y, pin4.angle, 2.54)
            for lx, ly in pin4_label_points:
                self.assertEqual(
                    sum(
                        1
                        for node in pushed.top_level_nodes("global_label")
                        if abs(float(as_text(find_child(node, "at")[1])) - lx) < 0.02
                        and abs(float(as_text(find_child(node, "at")[2])) - ly) < 0.02
                    ),
                    0,
                )

            for x, y in stale_points:
                key = (round(x, 2), round(y, 2))
                if key in current_points:
                    continue
                self.assertEqual(
                    sum(
                        1
                        for node in pushed.top_level_nodes("global_label")
                        if abs(float(as_text(find_child(node, "at")[1])) - x) < 0.02
                        and abs(float(as_text(find_child(node, "at")[2])) - y) < 0.02
                    ),
                    0,
                )
                self.assertEqual(
                    sum(
                        1
                        for node in pushed.top_level_nodes("no_connect")
                        if abs(float(as_text(find_child(node, "at")[1])) - x) < 0.02
                        and abs(float(as_text(find_child(node, "at")[2])) - y) < 0.02
                    ),
                    0,
                )

    def test_push_adds_missing_component_to_reused_child_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "child_add.json"
            root_sch = tmp / "root.kicad_sch"
            child_sch = tmp / "child.kicad_sch"
            payload = {
                "root_components": [],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILDDRV",
                                "description": "Existing child symbol",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "IN", "scope": "hierarchical"},
                                    {"number": "2", "name": "OUT", "net": "OUT", "scope": "hierarchical"},
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_A",
                        "sheet_id": "child",
                        "pin_map": [
                            {"name": "IN", "net": "TOP_IN_A", "scope": "global"},
                            {"name": "OUT", "net": "TOP_OUT_A", "scope": "global"},
                        ],
                    },
                    {
                        "instance_name": "CHILD_B",
                        "sheet_id": "child",
                        "pin_map": [
                            {"name": "IN", "net": "TOP_IN_B", "scope": "global"},
                            {"name": "OUT", "net": "TOP_OUT_B", "scope": "global"},
                        ],
                    },
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            before_push_child = KicadSchematic.from_file(child_sch)
            occupied_before = sync._occupied_right_edge(before_push_child)

            payload["sheet_definitions"][0]["components"].append(
                {
                    "refdes": "J_HDR",
                    "mpn": "CONN-2",
                    "description": "Late-added child connector",
                    "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                    "notes": "Added after initial layout.",
                    "connections": [
                        {"number": "1", "name": "P1", "net": "IN", "scope": "hierarchical"},
                        {"number": "2", "name": "P2", "net": "OUT", "scope": "hierarchical"},
                    ],
                }
            )
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed_child = KicadSchematic.from_file(child_sch)
            added_symbol = next(
                sym for sym in pushed_child.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_HDR"
            )
            self.assertEqual(
                get_property_value(added_symbol, "Footprint"),
                payload["sheet_definitions"][0]["components"][1]["footprint"],
            )
            added_at = find_child(added_symbol, "at")
            self.assertIsNotNone(added_at)
            added_component = sync._parse_component(payload["sheet_definitions"][0]["components"][1])
            added_symbol_def = sync._resolve_component_symbol(added_component, [])
            left_extent, _right_extent, _top_extent, _bottom_extent = sync._component_layout_metrics(
                sync.RenderComponent(component=added_component, symbol=added_symbol_def)
            )
            added_left_edge = float(as_text(added_at[1])) - left_extent
            self.assertGreater(
                added_left_edge,
                occupied_before + sync.NEW_COMPONENT_PLACEMENT_GAP_MM - 0.05,
            )

            instances = find_child(added_symbol, "instances")
            self.assertIsNotNone(instances)
            projects = [node for node in instances[1:] if isinstance(node, list) and list_head(node) == "project"]
            self.assertEqual(len(projects), 1)
            path_nodes = [node for node in projects[0][2:] if isinstance(node, list) and list_head(node) == "path"]
            self.assertEqual(len(path_nodes), 2)
            self.assertEqual(
                [as_text(find_child(node, "reference")[1]) for node in path_nodes],
                ["J1", "J2"],
            )

            labels = [node for node in pushed_child.top_level_nodes("hierarchical_label") if as_text(node[1]) in {"IN", "OUT"}]
            self.assertGreaterEqual(len(labels), 4)

    def test_push_removes_deleted_root_component_and_stub_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "delete_root.json"
            root_sch = tmp / "root.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "J_OLD",
                        "mpn": "CONN-2",
                        "description": "Connector to be deleted",
                        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                        "notes": "Temporary connector.",
                        "connections": [
                            {"number": "1", "name": "P1", "net": "NET_DELETE_A", "scope": "global"},
                            {"number": "2", "name": "P2", "net": "NET_DELETE_B", "scope": "global"},
                        ],
                    },
                    {
                        "refdes": "U_KEEP",
                        "mpn": "KEEP",
                        "description": "Keep symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "IN", "net": "NET_KEEP", "scope": "global"},
                        ],
                    },
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated = KicadSchematic.from_file(root_sch)
            stale_symbol = next(
                sym for sym in generated.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_OLD"
            )
            pin1 = find_symbol_pin(stale_symbol, "1", "", generated)
            pin2 = find_symbol_pin(stale_symbol, "2", "", generated)
            pin_points = {
                (pin1.x, pin1.y): candidate_label_points_for_pin(pin1.x, pin1.y, pin1.angle, 2.54),
                (pin2.x, pin2.y): candidate_label_points_for_pin(pin2.x, pin2.y, pin2.angle, 2.54),
            }

            payload["root_components"] = [payload["root_components"][1]]
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(root_sch)
            self.assertFalse(
                any(get_property_value(sym, "JsonRefdes") == "J_OLD" for sym in pushed.top_level_nodes("symbol"))
            )
            self.assertFalse(any(as_text(node[1]) == "NET_DELETE_A" for node in pushed.top_level_nodes("global_label")))
            self.assertFalse(any(as_text(node[1]) == "NET_DELETE_B" for node in pushed.top_level_nodes("global_label")))
            self.assertTrue(any(get_property_value(sym, "JsonRefdes") == "U_KEEP" for sym in pushed.top_level_nodes("symbol")))
            self.assertFalse(
                any(
                    isinstance(node, list) and list_head(node) == "symbol" and len(node) >= 2 and as_text(node[1]) == "generated:J_OLD"
                    for node in (pushed.find_first("lib_symbols") or [])[1:]
                )
            )

            for (pin_x, pin_y), label_points in pin_points.items():
                for wire in pushed.top_level_nodes("wire"):
                    pts = find_child(wire, "pts")
                    self.assertIsNotNone(pts)
                    xy = [node for node in pts[1:] if isinstance(node, list) and list_head(node) == "xy"]
                    coords = [(float(as_text(node[1])), float(as_text(node[2]))) for node in xy]
                    for label_x, label_y in label_points:
                        self.assertNotEqual(coords, [(pin_x, pin_y), (label_x, label_y)])
                        self.assertNotEqual(coords, [(label_x, label_y), (pin_x, pin_y)])

    def test_push_adds_child_connector_with_project_unique_reference_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "child_unique_refs.json"
            root_sch = tmp / "root.kicad_sch"
            child_sch = tmp / "child.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "J_DEBUG",
                        "mpn": "DEBUG-HDR",
                        "description": "Existing root connector",
                        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "A", "net": "DBG_A", "scope": "global"},
                            {"number": "2", "name": "B", "net": "DBG_B", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILDDRV",
                                "description": "Existing child symbol",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "IN", "scope": "hierarchical"},
                                    {"number": "2", "name": "OUT", "net": "OUT", "scope": "hierarchical"},
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_A",
                        "sheet_id": "child",
                        "pin_map": [
                            {"name": "IN", "net": "TOP_IN_A", "scope": "global"},
                            {"name": "OUT", "net": "TOP_OUT_A", "scope": "global"},
                        ],
                    },
                    {
                        "instance_name": "CHILD_B",
                        "sheet_id": "child",
                        "pin_map": [
                            {"name": "IN", "net": "TOP_IN_B", "scope": "global"},
                            {"name": "OUT", "net": "TOP_OUT_B", "scope": "global"},
                        ],
                    },
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            payload["sheet_definitions"][0]["components"].append(
                {
                    "refdes": "J_HDR",
                    "mpn": "CONN-2",
                    "description": "Late-added child connector",
                    "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                    "notes": "",
                    "connections": [
                        {"number": "1", "name": "P1", "net": "IN", "scope": "hierarchical"},
                        {"number": "2", "name": "P2", "net": "OUT", "scope": "hierarchical"},
                    ],
                }
            )
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(root_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed_root = KicadSchematic.from_file(root_sch)
            self.assertTrue(any(get_property_value(sym, "Reference") == "J1" for sym in pushed_root.top_level_nodes("symbol")))

            pushed_child = KicadSchematic.from_file(child_sch)
            added_symbol = next(
                sym for sym in pushed_child.top_level_nodes("symbol") if get_property_value(sym, "JsonRefdes") == "J_HDR"
            )
            instances = find_child(added_symbol, "instances")
            self.assertIsNotNone(instances)
            projects = [node for node in instances[1:] if isinstance(node, list) and list_head(node) == "project"]
            path_nodes = [node for node in projects[0][2:] if isinstance(node, list) and list_head(node) == "path"]
            self.assertEqual(
                [as_text(find_child(node, "reference")[1]) for node in path_nodes],
                ["J2", "J3"],
            )

    def test_push_removes_conflicting_same_position_local_and_global_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "tp.json"
            out_sch = tmp / "tp.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "TP_USB",
                        "description": "USB probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "USB_SIG", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            global_label = next(node for node in doc.top_level_nodes("global_label") if as_text(node[1]) == "USB_SIG")
            duplicate_local = [
                atom("label"),
                qstr("USB_SIG"),
                find_child(global_label, "at"),
                find_child(global_label, "effects"),
                [atom("uuid"), qstr("11111111-1111-1111-1111-111111111111")],
            ]
            doc.append_top_level(duplicate_local)
            doc.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            self.assertEqual(sum(1 for node in pushed.top_level_nodes("global_label") if as_text(node[1]) == "USB_SIG"), 1)
            self.assertEqual(sum(1 for node in pushed.top_level_nodes("label") if as_text(node[1]) == "USB_SIG"), 0)

    def test_generate_hierarchical_sheet_instances_connect_to_root_nets(self) -> None:
        cli = _kicad_cli_path()
        if not cli:
            self.skipTest("kicad-cli not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_json = tmp / "input.json"
            out_sch = tmp / "root.kicad_sch"
            out_net = tmp / "root.net"
            out_json.write_text((FIXTURES / "hierarchy_smoke.json").read_text(encoding="utf-8"), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(out_json), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            subprocess.run(
                [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out_net), str(out_sch)],
                check=True,
                cwd=str(tmp),
            )

            netlist = out_net.read_text(encoding="utf-8")
            self.assertIn('(name "DATA_BUS")', netlist)
            self.assertIn('(name "GND")', netlist)
            self.assertNotIn('(name "/LED_CHAIN_A/DATA_IN")', netlist)
            self.assertNotIn('(name "/LED_CHAIN_A/GND")', netlist)

    def test_generate_annotates_repeated_sheet_instances_with_unique_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "annotated.json"
            out_sch = tmp / "root.kicad_sch"
            child_sch = tmp / "child.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_ROOT",
                        "mpn": "ROOT",
                        "description": "Root symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "SIG", "net": "SIG"},
                        ],
                    }
                ],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILD",
                                "description": "Child symbol",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "IN", "scope": "hierarchical"},
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_A",
                        "sheet_id": "child",
                        "pin_map": [{"name": "IN", "net": "TOP_A", "scope": "global"}],
                    },
                    {
                        "instance_name": "CHILD_B",
                        "sheet_id": "child",
                        "pin_map": [{"name": "IN", "net": "TOP_B", "scope": "global"}],
                    },
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            root_text = out_sch.read_text(encoding="utf-8")
            child_text = child_sch.read_text(encoding="utf-8")

            self.assertRegex(root_text, r'"JsonRefdes"\s+"U_ROOT"')
            self.assertRegex(root_text, r'"Reference"\s+"U1"')
            self.assertRegex(child_text, r'"JsonRefdes"\s+"U_CHILD"')

            instance_matches = re.findall(
                r'\(\s*path\s+"(/[^"]+/[^"]+)"\s+\(\s*reference\s+"(U\d+)"\s*\)\s+\(\s*unit\s+1\s*\)\s*\)',
                child_text,
                re.S,
            )
            self.assertEqual(len(instance_matches), 2)
            self.assertEqual({ref for _path, ref in instance_matches}, {"U2", "U3"})
            for path, _ref in instance_matches:
                self.assertRegex(child_text, re.escape(path) + r'"\s+\(\s*page\s+"[23]"')

    def test_generate_uses_dynamic_grid_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "layout.json"
            out_sch = tmp / "layout.kicad_sch"

            tall_connections = [
                {"number": str(i), "name": f"PIN_{i}", "net": f"VERY_LONG_SIGNAL_NAME_{i}", "scope": "global"}
                for i in range(1, 13)
            ]
            payload = {
                "root_components": [
                    {
                        "refdes": "U_A_BIG",
                        "mpn": "BIG",
                        "description": "Large symbol",
                        "notes": "",
                        "connections": tall_connections,
                    },
                    {
                        "refdes": "U_B_SMALL1",
                        "mpn": "S1",
                        "description": "Small symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "A", "net": "A"},
                            {"number": "2", "name": "B", "net": "B"},
                        ],
                    },
                    {
                        "refdes": "U_C_SMALL2",
                        "mpn": "S2",
                        "description": "Small symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "A", "net": "A"},
                            {"number": "2", "name": "B", "net": "B"},
                        ],
                    },
                    {
                        "refdes": "U_D_SMALL3",
                        "mpn": "S3",
                        "description": "Small symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "A", "net": "A"},
                            {"number": "2", "name": "B", "net": "B"},
                        ],
                    },
                    {
                        "refdes": "U_E_ROW2",
                        "mpn": "R2",
                        "description": "Second row symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "A", "net": "A"},
                            {"number": "2", "name": "B", "net": "B"},
                        ],
                    },
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            positions = {}
            for sym in generated_doc.top_level_nodes("symbol"):
                ref = get_property_value(sym, "JsonRefdes") or get_property_value(sym, "Reference")
                at = find_child(sym, "at")
                if ref and at is not None:
                    positions[ref] = (float(as_text(at[1])), float(as_text(at[2])))

            self.assertGreater(positions["U_B_SMALL1"][0] - positions["U_A_BIG"][0], 60.0)
            self.assertGreater(positions["U_E_ROW2"][1] - positions["U_A_BIG"][1], 35.0)

    def test_generate_places_sheet_instances_after_root_component_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "sheet_layout.json"
            out_sch = tmp / "sheet_layout.kicad_sch"

            wide_connections = [
                {"number": str(i), "name": f"PIN_{i}", "net": f"VERY_LONG_NET_NAME_FOR_LAYOUT_{i}", "scope": "global"}
                for i in range(1, 13)
            ]
            payload = {
                "root_components": [
                    {
                        "refdes": f"U_{idx:02d}",
                        "mpn": f"ROOT_{idx}",
                        "description": "Wide root symbol",
                        "notes": "",
                        "connections": wide_connections,
                    }
                    for idx in range(5)
                ],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILD",
                                "description": "Child",
                                "notes": "",
                                "connections": [
                                    {"number": "1", "name": "IN", "net": "IN", "scope": "hierarchical"},
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_0",
                        "sheet_id": "child",
                        "pin_map": [{"name": "IN", "net": "TOP_IN", "scope": "global"}],
                    }
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            symbol_right_edge = 0.0
            for sym in generated_doc.top_level_nodes("symbol"):
                at = find_child(sym, "at")
                if at is None:
                    continue
                symbol_right_edge = max(symbol_right_edge, float(as_text(at[1])))

            sheets = generated_doc.top_level_nodes("sheet")
            self.assertEqual(len(sheets), 1)
            sheet_at = find_child(sheets[0], "at")
            self.assertIsNotNone(sheet_at)
            self.assertGreater(float(as_text(sheet_at[1])), symbol_right_edge + 20.0)

    def test_generate_expands_sheet_box_for_many_interface_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "sheet_size.json"
            out_sch = tmp / "sheet_size.kicad_sch"
            pin_names = [f"PHASE_{idx:02d}" for idx in range(10)]
            payload = {
                "root_components": [],
                "sheet_definitions": [
                    {
                        "sheet_id": "child",
                        "file": "child.kicad_sch",
                        "components": [
                            {
                                "refdes": "U_CHILD",
                                "mpn": "CHILD",
                                "description": "Child",
                                "notes": "",
                                "connections": [
                                    {"number": str(idx + 1), "name": name, "net": name, "scope": "hierarchical"}
                                    for idx, name in enumerate(pin_names)
                                ],
                            }
                        ],
                    }
                ],
                "sheet_instances": [
                    {
                        "instance_name": "CHILD_BIG",
                        "sheet_id": "child",
                        "pin_map": [{"name": name, "net": f"TOP_{name}", "scope": "global"} for name in pin_names],
                    }
                ],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            sheets = generated_doc.top_level_nodes("sheet")
            self.assertEqual(len(sheets), 1)
            sheet = sheets[0]
            sheet_at = find_child(sheet, "at")
            sheet_size = find_child(sheet, "size")
            self.assertIsNotNone(sheet_at)
            self.assertIsNotNone(sheet_size)
            y0 = float(as_text(sheet_at[2]))
            height = float(as_text(sheet_size[2]))
            self.assertGreater(height, 30.0)

            pin_nodes = [node for node in sheet[1:] if isinstance(node, list) and list_head(node) == "pin"]
            self.assertEqual(len(pin_nodes), len(pin_names))
            max_pin_y = max(float(as_text(find_child(node, "at")[2])) for node in pin_nodes)
            self.assertLess(max_pin_y, y0 + height)

    def test_generate_widens_symbol_body_for_long_pin_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "wide_symbol.json"
            out_sch = tmp / "wide_symbol.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "U_WIDE",
                        "mpn": "WIDE",
                        "description": "Wide symbol",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "VERY_LONG_LEFT_SIDE_FUNCTION_NAME", "net": "N1"},
                            {"number": "2", "name": "VERY_LONG_RIGHT_SIDE_FUNCTION_NAME", "net": "N2"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            generated_doc = KicadSchematic.from_file(out_sch)
            lib_symbols = generated_doc.find_first("lib_symbols")
            self.assertIsNotNone(lib_symbols)
            lib_symbol = next(node for node in lib_symbols[1:] if isinstance(node, list) and list_head(node) == "symbol")
            body_symbol = next(
                node for node in lib_symbol[2:] if isinstance(node, list) and list_head(node) == "symbol" and as_text(node[1]).endswith("_0_1")
            )
            rectangle = find_child(body_symbol, "rectangle")
            self.assertIsNotNone(rectangle)
            start = find_child(rectangle, "start")
            end = find_child(rectangle, "end")
            self.assertIsNotNone(start)
            self.assertIsNotNone(end)
            width = float(as_text(end[1])) - float(as_text(start[1]))
            self.assertGreater(width, 40.0)

    def test_push_sweeps_stale_label_moved_away_from_pin(self) -> None:
        # A label dragged away from its pin (manual cleanup) survives pin-local
        # replacement; the name-based sweep must still remove it once the net
        # is renamed in JSON.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "stale_sweep.json"
            out_sch = tmp / "stale_sweep.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "TP_EN",
                        "description": "Enable probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "LED_EN", "scope": "global"},
                        ],
                    },
                    {
                        "refdes": "TP_GND",
                        "description": "Ground probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "GND", "scope": "global"},
                        ],
                    },
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            moved = next(node for node in doc.top_level_nodes("global_label") if as_text(node[1]) == "LED_EN")
            at = find_child(moved, "at")
            at[1] = atom("199.39")
            at[2] = atom("199.39")
            doc.to_file(out_sch)

            payload["root_components"][0]["connections"][0]["net"] = "LED_ENABLE"
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertIn("Removed stale global label 'LED_EN'", result.stdout)

            pushed = KicadSchematic.from_file(out_sch)
            for head in ("label", "global_label", "hierarchical_label"):
                self.assertFalse(
                    any(as_text(node[1]) == "LED_EN" for node in pushed.top_level_nodes(head)),
                    f"stale LED_EN {head} should have been swept",
                )
            self.assertTrue(
                any(as_text(node[1]) == "LED_ENABLE" for node in pushed.top_level_nodes("global_label")),
            )
            # Untouched live label stays put.
            self.assertTrue(
                any(as_text(node[1]) == "GND" for node in pushed.top_level_nodes("global_label")),
            )

    def test_push_keeps_manual_labels_for_live_nets(self) -> None:
        # A user-placed extra label naming a live net must survive the sweep.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "manual_label.json"
            out_sch = tmp / "manual_label.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "TP_SIG",
                        "description": "Signal probe point",
                        "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "SIG", "scope": "global"},
                        ],
                    }
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            doc = KicadSchematic.from_file(out_sch)
            existing = next(node for node in doc.top_level_nodes("global_label") if as_text(node[1]) == "SIG")
            extra = copy.deepcopy(existing)
            extra_at = find_child(extra, "at")
            extra_at[1] = atom("150.62")
            extra_at[2] = atom("150.62")
            doc.append_top_level(extra)
            doc.to_file(out_sch)

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "push", "--json", str(json_path), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )

            pushed = KicadSchematic.from_file(out_sch)
            sig_labels = [node for node in pushed.top_level_nodes("global_label") if as_text(node[1]) == "SIG"]
            self.assertEqual(len(sig_labels), 2)

    def test_lint_single_pin_nets_counts_scopes_and_instances(self) -> None:
        design = sync.DesignData(
            root_components=[
                sync.Component(
                    refdes="U1",
                    connections=[
                        sync.PinConn(number="1", name="A", net="OK_NET"),
                        sync.PinConn(number="2", name="B", net="TYPO_NET"),
                        sync.PinConn(number="3", name="C", net="GND"),
                        sync.PinConn(number="4", name="D", net="__NC__"),
                    ],
                ),
                sync.Component(
                    refdes="U2",
                    connections=[
                        sync.PinConn(number="1", name="A", net="OK_NET"),
                    ],
                ),
            ],
            sheet_definitions=[
                sync.SheetDefinition(
                    sheet_id="ch",
                    file="ch.kicad_sch",
                    components=[
                        sync.Component(
                            refdes="R1",
                            connections=[
                                sync.PinConn(number="1", name="1", net="CH_IN", scope="hierarchical"),
                                sync.PinConn(number="2", name="2", net="LONELY_LOCAL", scope="local"),
                                sync.PinConn(number="3", name="3", net="GND"),
                            ],
                        ),
                    ],
                ),
            ],
            sheet_instances=[
                sync.SheetInstance(
                    instance_name="CH_A",
                    sheet_id="ch",
                    pin_map=[sync.SheetPinMap(name="CH_IN", net="EN_ONLY_HERE")],
                ),
            ],
        )
        findings = sync.lint_single_pin_nets(design)
        flagged = {net for net, _ in findings}
        # TYPO_NET: one pin in root. LONELY_LOCAL: one pin per sheet instance.
        # EN_ONLY_HERE: pin_map's parent net driven by nothing else.
        self.assertEqual(flagged, {"TYPO_NET", "LONELY_LOCAL", "EN_ONLY_HERE"})
        # GND has root pin + one per instance; OK_NET has two root pins.
        self.assertNotIn("GND", flagged)
        self.assertNotIn("OK_NET", flagged)

    def test_cli_push_reports_single_pin_net_in_exit_code(self) -> None:
        from kicadsync import cli as kicadsync_cli

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "health.json"
            out_sch = tmp / "health.kicad_sch"
            payload = {
                "root_components": [
                    {
                        "refdes": "R1",
                        "description": "resistor",
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "SIG_A", "scope": "global"},
                            {"number": "2", "name": "2", "net": "GND", "scope": "global"},
                        ],
                    },
                    {
                        "refdes": "R2",
                        "description": "resistor",
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
                        "notes": "",
                        "connections": [
                            {"number": "1", "name": "1", "net": "SIG_B", "scope": "global"},
                            {"number": "2", "name": "2", "net": "GND", "scope": "global"},
                        ],
                    },
                ],
                "sheet_definitions": [],
                "sheet_instances": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            # SIG_A and SIG_B are each single-pin (the classic typo): push
            # succeeds but the built-in health check must flag it via exit code.
            status = kicadsync_cli.main(["push", "--json", str(json_path), "--sch", str(out_sch)])
            self.assertEqual(status, 1)

            # Join the two halves into one net: now healthy (exit 0 whether or
            # not kicad-cli is installed).
            payload["root_components"][1]["connections"][0]["net"] = "SIG_A"
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            status = kicadsync_cli.main(["push", "--json", str(json_path), "--sch", str(out_sch)])
            self.assertEqual(status, 0)

    def test_kicad_cli_validation_smoke(self) -> None:
        cli = _kicad_cli_path()
        if not cli:
            self.skipTest("kicad-cli not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_sch = tmp / "cli_smoke.kicad_sch"
            out_json = tmp / "input.json"
            out_json.write_text((FIXTURES / "hierarchy_smoke.json").read_text(encoding="utf-8"), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "kicadsync.json_schem_sync", "generate", "--json", str(out_json), "--sch", str(out_sch)],
                check=True,
                cwd=str(REPO_ROOT),
            )
            self.assertTrue(out_sch.exists())

            subprocess.run(
                [cli, "sch", "export", "netlist", "--format", "kicadsexpr", str(out_sch)],
                check=True,
                cwd=str(tmp),
            )


if __name__ == "__main__":
    unittest.main()
