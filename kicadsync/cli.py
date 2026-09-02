"""Unified ``kicadsync`` command-line interface.

Subcommands:

  new <name>       Scaffold a fresh PCB project (design JSON + KiCad project + docs)
  push             JSON -> schematic (auto-generates on first run), then health-check
  check            Re-run the health check alone (JSON lint + netlist audit) — push/generate
                   already run it; use this after manual edits in KiCad
  pull             Schematic -> JSON (net labels)
  generate         Rebuild schematic from scratch (--force if it exists)
  place            Cluster + place PCB footprints (design-JSON comment groups
                   + net-connectivity inference); run KiCad's Update PCB from
                   Schematic (F8) and save first. Locked footprints stay put.
  list-libs        Dump installed KiCad symbols to the shared cache
  jlc-report       Price/stock BOM report for a design JSON
  jlc ...          Fetch/search the JLCPCB parts catalog
  config           Show or set machine-local paths (parts library, cache)

``generate``/``push``/``pull`` can run with no arguments from inside a project
folder scaffolded by ``new`` — the design JSON and schematic are discovered by
convention (``<name>.json`` next to ``kicad/<name>.kicad_sch``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from . import jlc_catalog, jlc_price_report, json_schem_sync, paths, pcb_place
from .kicad_sch_sexp import as_text, list_head, parse_one

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def _looks_like_design(path: Path) -> bool:
    try:
        data = json.loads(json_schem_sync.strip_json_comments(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "root_components" in data


def discover_project(cwd: Optional[Path] = None) -> Tuple[Path, Path]:
    """Find (design_json, schematic) for the project at/above cwd."""
    start = (cwd or Path.cwd()).resolve()
    for base in (start, *start.parents):
        candidates = [p for p in sorted(base.glob("*.json")) if _looks_like_design(p)]
        if not candidates:
            continue
        if len(candidates) > 1:
            names = ", ".join(p.name for p in candidates)
            raise SystemExit(
                f"Multiple design JSON files in {base} ({names}); "
                "pass --json/--sch explicitly."
            )
        design = candidates[0]
        return design, base / "kicad" / f"{design.stem}.kicad_sch"
    raise SystemExit(
        "No design JSON found here or in any parent folder. Run inside a project "
        "created by `kicadsync new`, or pass --json/--sch explicitly."
    )


def _flag_value(argv: List[str], flag: str) -> Optional[str]:
    """Value of --flag, handling both '--flag x' and '--flag=x' forms."""
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:]
    return None


def _with_project_defaults(argv: List[str]) -> List[str]:
    if _flag_value(argv, "--json") is not None or _flag_value(argv, "--sch") is not None:
        return argv
    design, sch = discover_project()
    print(f"[project] json: {design}")
    print(f"[project] sch:  {sch}")
    return [*argv, "--json", str(design), "--sch", str(sch)]


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def _render(template_name: str, name: str) -> str:
    text = (TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    return text.replace("{{NAME}}", name)


def cmd_new(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="kicadsync new", description="Scaffold a new PCB project")
    parser.add_argument("name", help="Project name (folder + design JSON + KiCad project name)")
    parser.add_argument("--dir", type=Path, default=Path.cwd(), help="Parent directory (default: cwd)")
    args = parser.parse_args(argv)

    name = args.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise SystemExit(
            f"Bad project name {name!r}: use letters, digits, '.', '_' or '-' "
            "(it becomes folder, file, and KiCad project names)"
        )
    root = (args.dir / name).resolve()
    if root.exists():
        raise SystemExit(f"{root} already exists")

    (root / "kicad").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "firmware").mkdir()
    (root / ".claude" / "agents").mkdir(parents=True)

    (root / f"{name}.json").write_text(_render("design.json.tpl", name), encoding="utf-8")
    (root / "README.md").write_text(_render("README.md.tpl", name), encoding="utf-8")
    (root / "CLAUDE.md").write_text(_render("CLAUDE.md.tpl", name), encoding="utf-8")
    (root / ".gitignore").write_text(_render("gitignore.tpl", name), encoding="utf-8")
    (root / "kicad" / f"{name}.kicad_pro").write_text(_render("kicad_pro.tpl", name), encoding="utf-8")
    (root / "kicad" / "fp-lib-table").write_text(_render("fp-lib-table.tpl", name), encoding="utf-8")
    for agent in sorted((TEMPLATES_DIR / "agents").glob("*.md")):
        shutil.copy(agent, root / ".claude" / "agents" / agent.name)

    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(root)], check=False)

    lib = paths.lib_dir()
    print(f"Created {root}")
    print(f"  1. Describe your circuit in {name}.json (see CLAUDE.md for the schema)")
    print(f"  2. Run `kicadsync push` from inside the folder to build kicad/{name}.kicad_sch")
    print(f"  3. Open kicad/{name}.kicad_pro in KiCad and route the board")
    if lib is None:
        print("NOTE: parts library not configured — run: kicadsync config lib_dir /path/to/kicad-lib")
    return 0


# ---------------------------------------------------------------------------
# Check: audit the real exported netlist against the design JSON
# ---------------------------------------------------------------------------

def _kicad_cli() -> Optional[str]:
    mac_app = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if mac_app.exists():
        return str(mac_app)
    return shutil.which("kicad-cli")


def _netlist_nets(netlist_path: Path) -> dict:
    """Parse a KiCad netlist export into {net_name: node_count}."""
    doc = parse_one(netlist_path.read_text(encoding="utf-8"))
    nets: dict = {}
    for section in doc:
        if list_head(section) != "nets":
            continue
        for net in section[1:]:
            if list_head(net) != "net":
                continue
            name = None
            nodes = 0
            for child in net[1:]:
                head = list_head(child)
                if head == "name":
                    name = as_text(child[1])
                elif head == "node":
                    nodes += 1
            if name is not None:
                nets[name] = nodes
    return nets


def _design_expected_nets(design) -> set:
    """Net names the JSON expects to exist, excluding NC pins and hierarchical
    interface names (those dissolve into the parent net named by pin_map)."""
    expected = set()
    all_components = list(design.root_components)
    for sheet_def in design.sheet_definitions:
        all_components.extend(sheet_def.components)
    for comp in all_components:
        for conn in comp.connections:
            if conn.net and conn.net != "__NC__" and conn.scope != "hierarchical":
                expected.add(conn.net)
    for inst in design.sheet_instances:
        for pin in inst.pin_map:
            if pin.net and pin.net != "__NC__":
                expected.add(pin.net)
    return expected


def run_health_check(design_path: Path, sch_path: Path) -> int:
    """JSON single-pin lint + netlist audit (via kicad-cli) in one pass.

    Returns 0 clean, 1 problems found, 2 netlist audit unavailable
    (kicad-cli missing) with the JSON lint clean.
    """
    design = json_schem_sync.design_from_json(design_path)

    problems = 0
    linted_nets = set()
    for net, message in json_schem_sync.lint_single_pin_nets(design):
        print(f"[check] {message}")
        linted_nets.add(net)
        problems += 1

    for message in json_schem_sync.lint_stacked_labels(design, sch_path):
        print(f"[check] {message}")
        problems += 1

    cli = _kicad_cli()
    if cli is None:
        print("[check] kicad-cli not found — skipping netlist audit (install KiCad to enable it)")
        if problems:
            print(f"[check] {problems} problem(s) found")
            return 1
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        net_file = Path(tmp) / "check.net"
        result = subprocess.run(
            [cli, "sch", "export", "netlist", str(sch_path), "-o", str(net_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not net_file.exists():
            print(result.stdout + result.stderr)
            raise SystemExit("[check] kicad-cli netlist export failed")
        nets = _netlist_nets(net_file)

    expected = _design_expected_nets(design)
    # Base name = last path segment; sub-sheet local nets export as /INSTANCE/NET.
    actual = {}
    for name, node_count in nets.items():
        if name.startswith("unconnected-"):
            continue
        actual.setdefault(name.rsplit("/", 1)[-1], []).append((name, node_count))

    for base in sorted(set(actual) - expected):
        for full, _ in actual[base]:
            print(f"[check] STALE net in schematic, not in JSON: {full}")
            print("        (leftover label from a renamed/removed net — re-run `kicadsync push`)")
            problems += 1
    for base in sorted(expected - set(actual)):
        print(f"[check] UNWIRED net in JSON, missing from schematic: {base}")
        print("        (run `kicadsync push`, or the labels were deleted in KiCad)")
        problems += 1
    for base, entries in sorted(actual.items()):
        if base in linted_nets:
            continue  # already reported by the JSON lint above
        for full, node_count in entries:
            if node_count == 1:
                print(f"[check] SINGLE-PIN net: {full} (net splits or deleted labels in the schematic?)")
                problems += 1

    if problems:
        print(f"[check] {problems} problem(s) found")
        return 1
    print(f"[check] OK: {len(nets)} nets match the design JSON")
    return 0


def cmd_check(argv: List[str]) -> int:
    argv = _with_project_defaults(argv)
    design_path = Path(_flag_value(argv, "--json"))
    sch_path = Path(_flag_value(argv, "--sch"))
    if not sch_path.exists():
        raise SystemExit(f"[check] schematic not found: {sch_path} (run `kicadsync push` first)")
    return run_health_check(design_path, sch_path)


def _post_sync_health(rest: List[str]) -> int:
    """Run the health check after a successful push/generate.

    A missing kicad-cli must not fail the sync, so status 2 maps to 0.
    """
    json_arg = _flag_value(rest, "--json")
    sch_arg = _flag_value(rest, "--out-sch") or _flag_value(rest, "--sch")
    if json_arg is None or sch_arg is None or not Path(sch_arg).exists():
        return 0
    status = run_health_check(Path(json_arg), Path(sch_arg))
    return 0 if status == 2 else status


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def cmd_config(argv: List[str]) -> int:
    if not argv:
        lib = paths.lib_dir()
        print(f"config file: {paths.CONFIG_PATH}")
        print(f"cache_dir:   {paths.cache_dir()}")
        print(f"lib_dir:     {lib if lib else '(not set)'}")
        print(f"libs.json:   {paths.libs_json_path()}"
              f" ({'present' if paths.libs_json_path().exists() else 'missing — run `kicadsync list-libs`'})")
        jlc = paths.jlc_cache_dir() / "normalized"
        print(f"jlc catalog: {jlc} ({'present' if jlc.exists() else 'missing — run `kicadsync jlc fetch-all`'})")
        return 0
    if len(argv) == 2:
        key, value = argv
        if key not in ("lib_dir", "cache_dir"):
            raise SystemExit(f"Unknown config key: {key} (expected lib_dir or cache_dir)")
        paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[key] = str(Path(value).expanduser().resolve())
        paths.CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"{key} = {data[key]}")
        return 0
    raise SystemExit("Usage: kicadsync config [<key> <value>]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

USAGE = __doc__


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "new":
        return cmd_new(rest)
    if cmd == "config":
        return cmd_config(rest)
    if cmd == "generate":
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        rest = _with_project_defaults(rest)
        sch_arg = _flag_value(rest, "--sch")
        if sch_arg and Path(sch_arg).exists() and not force:
            raise SystemExit(
                f"[generate] {sch_arg} already exists. `generate` rebuilds it from "
                "scratch, discarding manual schematic edits. Use `kicadsync push` "
                "to update it, or `kicadsync generate --force` to really regenerate."
            )
        status = json_schem_sync.main(["generate", *rest]) or 0
        return status if status else _post_sync_health(rest)
    if cmd == "pull":
        rest = _with_project_defaults(rest)
        if _flag_value(rest, "--out-json") is None:
            print(
                "[pull] NOTE: rewriting the design JSON in place — comments and "
                "formatting are not preserved (use --out-json FILE to write elsewhere)"
            )
        return json_schem_sync.main(["pull", *rest]) or 0
    if cmd == "push":
        rest = _with_project_defaults(rest)
        sch_arg = _flag_value(rest, "--sch")
        if sch_arg and not Path(sch_arg).exists():
            print(f"[push] {sch_arg} does not exist yet — running generate instead")
            status = json_schem_sync.main(["generate", *rest]) or 0
        else:
            status = json_schem_sync.main(["push", *rest]) or 0
        return status if status else _post_sync_health(rest)
    if cmd == "place":
        rest = _with_project_defaults(rest)
        if _flag_value(rest, "--pcb") is None:
            sch_arg = _flag_value(rest, "--sch")
            if sch_arg:
                rest = [*rest, "--pcb", str(Path(sch_arg).with_suffix(".kicad_pcb"))]
        return pcb_place.main(rest) or 0
    if cmd == "check":
        return cmd_check(rest)
    if cmd == "list-libs":
        if "--output" not in rest:
            out = paths.libs_json_path()
            out.parent.mkdir(parents=True, exist_ok=True)
            rest = [*rest, "--output", str(out)]
            print(f"[list-libs] writing {out}")
        return json_schem_sync.main(["list-libs", *rest]) or 0
    if cmd == "jlc-report":
        return jlc_price_report.main(rest) or 0
    if cmd == "jlc":
        return jlc_catalog.main(rest) or 0

    print(USAGE)
    raise SystemExit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
