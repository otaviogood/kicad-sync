# kicad-tools

**Vibe-code your PCBs.** Describe a schematic in a single JSON file — components,
pins, nets — and sync it to a real KiCad 9 project. Designed so that an AI
coding assistant (or a human in a text editor) does everything upstream of
layout, and you only open KiCad to route the board.

**New here? Read [GUIDE.md](GUIDE.md)** — a step-by-step walkthrough from
empty folder to ordered board.

Making a new PCB becomes as easy as starting a Python project:

```bash
kicadsync new blinky
cd blinky
# edit blinky.json — add your MCU, regulator, LEDs, connectors...
kicadsync push                 # JSON -> kicad/blinky.kicad_sch
kicadsync jlc-report blinky.json   # JLCPCB price & stock for the whole BOM
open kicad/blinky.kicad_pro    # route the board in KiCad
```

## What's in the box

| Command | What it does |
| --- | --- |
| `kicadsync new <name>` | Scaffold a project: design JSON, KiCad project, README, CLAUDE.md, part-onboarding agents |
| `kicadsync push` | Sync design JSON into the schematic (generates it on first run; updates properties + net labels, sweeps stale labels), then health-check: single-pin-net lint, stacked-label lint, plus exported-netlist audit. Labels/stubs the tool places carry ownership UUIDs, so when a symbol's pins move (re-render, symbol moved in KiCad) the old labels are swept wherever they ended up instead of stacking or silently merging nets |
| `kicadsync check` | Re-run the health check alone — useful after deleting/moving labels by hand in KiCad |
| `kicadsync pull` | Read net-label edits made in KiCad back into the JSON |
| `kicadsync generate --force` | Rebuild the schematic from scratch, discarding manual edits. Symbols are laid out one block per `// ----` comment group with a bold heading above each — the same grouping `place` uses on the PCB |
| `kicadsync place` | Cluster + place PCB footprints: groups come from the design JSON's `// ----` comment separators, and small parts snap next to the IC/connector pin sharing their most-specific net (decoupling caps land at their pins automatically). Each group gets a labelled box on the `User.Comments` layer. Run KiCad's *Update PCB from Schematic* (F8) and save first. Locked footprints are never moved — lock what you've finalized and re-run. `--gap` sets group spacing, `--no-outlines` skips the boxes |
| `kicadsync list-libs` | Scan installed KiCad symbol libraries into a local metadata cache (symbols get reused during generation) |
| `kicadsync jlc-report <design.json>` | BOM report with JLCPCB/LCSC unit price and live stock, sorted by cost and by scarcity |
| `kicadsync jlc fetch-all` / `search-mpn` | Download/search the [jlcparts](https://github.com/yaqwsx/jlcparts) static catalog locally |
| `kicadsync config` | Show or set machine-local paths (parts library, caches) |

The JSON model supports hierarchical sheets with multiple instances (e.g. 32
identical driver channels defined once), global/local/hierarchical net scopes,
explicit no-connects (`__NC__`), DNP parts, and non-BOM parts like test points.
Schema: [`schema/design.schema.json`](schema/design.schema.json).

[`parts-library/`](parts-library/) holds the custom footprints, 3D models, and
(locally) datasheets that projects share — see its README. Fork the repo and
it becomes your library.

Inside `kicadsync push`, a native KiCad 9 S-expression parser/writer
(`kicad_sch_sexp.py` / `kicad_sch_model.py` / `kicad_sch_ops.py`) round-trips
schematic files while preserving any blocks it doesn't understand — your KiCad
edits survive the sync.

## Install

```bash
git clone https://github.com/otaviogood/kicad-tools
uv tool install --editable ./kicad-tools   # puts `kicadsync` on your PATH
```

Or for hacking on the tools themselves: `cd kicad-tools && uv sync`, then
`uv run kicadsync ...`.

Requires [uv](https://docs.astral.sh/uv/), Python 3.10+, and KiCad 9
(KiCad 8 files also parse).

## One-time setup

```bash
# Point at the parts library (ships in this repo; grows as you onboard parts)
kicadsync config lib_dir /path/to/kicad-tools/parts-library

# Cache symbol metadata from your installed KiCad libraries
kicadsync list-libs

# (Optional, ~2 GB) local JLC catalog for offline price/stock reports
kicadsync jlc fetch-all
```

In KiCad → Preferences → Configure Paths, add `KICADSYNC_LIB` pointing at your
parts library and `MY_3D_MODELS` at `${KICADSYNC_LIB}/3dshapes`. Scaffolded
projects reference footprints as `parts:<MPN>` through those variables, so
projects stay machine-independent.

## The workflow, in words

1. **Describe** the circuit in `<name>.json`. Each component lists its refdes,
   MPN + LCSC number, footprint, and a `connections` array mapping every pin
   number to a net name. Comments (`//`) are allowed.
2. **Sync** with `kicadsync push`. Symbols come from your installed KiCad
   libraries when the MPN matches (via the `list-libs` cache); unmatched parts
   get placeholder symbols marked `NOLIB` for review. On first generation the
   schematic is laid out by your JSON's `// ----` comment groups — one block
   per group with a bold heading, in the same order as the file.
3. **Route** in KiCad. The generated schematic drives the netlist and PCB.
   After *Update PCB from Schematic* (F8), `kicadsync place` turns the heap of
   footprints into a starting arrangement — one cluster per JSON comment
   group, satellites hugging the pins they connect to, clusters that share
   nets adjacent. Every group is outlined and labelled on `User.Comments`
   so you can see the structure at a glance; those boxes are documentation
   only (not plotted in fab output) and each run replaces the previous
   run's, leaving your own drawings on that layer untouched. Refine by
   hand, lock the footprints you're happy with (locked parts are never
   moved), re-run `place` any time.
4. **Iterate.** Re-run `push` after JSON edits — it updates properties and
   labels without destroying manual schematic cleanup, and `pull` brings
   KiCad-side net renames back into the JSON.

## AI-assisted part onboarding

`kicadsync new` drops two Claude Code agents into each project:

- **datasheet-footprint-pages** — finds the official datasheet for an MPN,
  saves it into your parts library, and renders the package/land-pattern pages
  to PNG.
- **kicad-part-builder** — produces a validated `.kicad_mod` footprint for an
  MPN (downloaded from official sources when possible, generated from the
  datasheet otherwise), with pad numbering cross-checked against your design
  JSON.

## Tests

```bash
uv run python -m unittest discover tests
```

Covers parser round-trips (unknown S-expression blocks preserved),
generate/push/pull hierarchy workflows, and — when KiCad is installed —
netlist export via `kicad-cli` as an end-to-end validity check.

## License

MIT
