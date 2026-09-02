# {{NAME}} — PCB project

This is a **JSON-first PCB project**. The schematic is never drawn by hand:
[`{{NAME}}.json`](./{{NAME}}.json) is the single source of truth for components,
pins, and nets, and the `kicadsync` CLI generates/updates the KiCad schematic
from it. The human routes the PCB in KiCad; everything upstream of layout is
done by editing the JSON.

## Core loop

1. Edit `{{NAME}}.json` (add/change components, pins, nets).
2. `kicadsync push` — updates `kicad/{{NAME}}.kicad_sch` (first run generates
   it, grouping symbols into one labelled block per `// ----` comment group),
   removes labels for nets that no longer exist in the JSON, then runs a
   health check: a single-pin-net lint on the JSON plus a real netlist export
   compared against the design. Exit code 0 means schematic and JSON agree;
   nonzero means `[check]` findings were printed — fix them before proceeding.
3. `kicadsync jlc-report {{NAME}}.json` — check JLC price/stock for every part.
4. The user opens `kicad/{{NAME}}.kicad_pro`, updates the PCB from the
   schematic (F8), saves. Then `kicadsync place` arranges the footprints:
   one cluster per `// ----` comment group in the JSON, passives placed at
   the IC/connector pins their nets connect to, connected clusters adjacent,
   and each group outlined + labelled on the `User.Comments` layer.
   The user refines in KiCad and locks footprints to protect them from the
   next `place` run; routing stays manual.
5. If the user edited net labels in KiCad: `kicadsync pull` syncs them back to
   JSON (note: rewrites the file — JSON comments are lost). After deleting or
   moving labels by hand in KiCad, `kicadsync check` re-runs the health check
   without pushing.

Never hand-edit generated `.kicad_sch` files. The `.kicad_pcb` belongs to the
user/KiCad — do not regenerate or overwrite it.

Renaming or removing nets in the JSON is handled by `push`: new labels are
placed and stale ones (names absent from the JSON) are swept automatically,
including labels the user moved during manual cleanup. Tool-placed labels and
pin stubs carry ownership UUIDs (`c0de9ace-…`), so when a symbol's pins move —
a re-rendered symbol, or one repositioned in KiCad — the old labels are swept
wherever they drifted and re-placed at the pins; hand-placed labels (fresh
KiCad UUIDs) are never touched. Watch push's `[check]`
output — `SINGLE-PIN net` findings are almost always a net-name typo in the
JSON (`LED_EN` vs `LED_ENA` makes two half-nets with no other error).

## Design JSON schema (summary)

Top level: `{ "root_components": [...], "sheet_definitions": [...], "sheet_instances": [...] }`

- `root_components[]` — parts on the root sheet.
- `sheet_definitions[]` — reusable child sheets: `{ "sheet_id", "file": "x.kicad_sch", "components": [...] }`.
- `sheet_instances[]` — placements of a definition on the root: `{ "instance_name", "sheet_id", "pin_map": [{ "name", "net", "scope" }] }`. One definition can be instanced many times (arrays of identical channels).

Component:

```json
{
  "refdes": "U_MCU",
  "mpn": "RP2350B",
  "LCSC": "C42415655",
  "footprint": "Package_DFN_QFN:QFN-80_10x10mm",
  "dnp": false,
  "description": "Main microcontroller",
  "notes": "Engineering notes for reviewers and future you.",
  "connections": [
    { "number": "1", "name": "GPIO0", "net": "LED_DATA", "scope": "global" }
  ]
}
```

Rules and conventions:

- `refdes` names are semantic: `U_MCU`, `R_CC1`, `C_VBUS`, `J_USB`, `TP_GND_1`.
- `mpn` and `LCSC` go together — if one is present, the other must be too. The
  LCSC number (`C…`) disambiguates generic MPNs and drives `jlc-report`.
  Prefer JLC basic/preferred parts; verify numbers with `kicadsync jlc search-mpn <MPN>`.
- `connections[].number` must match the symbol/footprint pin numbering exactly
  (including alphanumeric pads like `A1`, `EP`, `SH`). It is the authoritative
  pin numbering for the whole project.
- `net` scope defaults to `global`; use `"scope": "local"` for nets private to
  a sheet, `"hierarchical"` for child-sheet interface pins.
- No-connect pins: `{ "number": "12", "name": "NC", "net": "__NC__" }`.
- Test points: 1-pin components with a `footprint` and no `mpn` (non-BOM).
- DNP parts: `"dnp": true` keeps the footprint on the PCB, marks Do-Not-Populate,
  and excludes from BOM.
- Custom footprints from the personal parts library are referenced as
  `parts:<MPN>` (the `parts` lib nickname is wired up in `kicad/fp-lib-table`).
- The JSON may contain `//` and `/* */` comments; tools strip them.

Full JSON Schema: `schema/design.schema.json` in the kicad-tools repo.

## Onboarding a new part

When adding a component whose footprint doesn't exist yet in KiCad's standard
libraries or the personal parts library:

1. Use the `datasheet-footprint-pages` agent to fetch the official datasheet
   into the parts library (`kicadsync config` shows `lib_dir`), saved as
   `<lib_dir>/datasheets/<MPN>/<MPN>.pdf` plus footprint-page PNGs.
2. Use the `kicad-part-builder` agent to produce `<lib_dir>/footprints.pretty/<MPN>.kicad_mod`,
   matching pad numbers to this project's `connections[].number`.
3. Reference it in the JSON as `"footprint": "parts:<MPN>"`.

## Environment

- `kicadsync config` prints the resolved parts-library and cache paths.
- Symbol metadata cache: `kicadsync list-libs` (rescans installed KiCad libraries).
- JLC catalog cache: `kicadsync jlc fetch-all` (rarely needed; it's big).
- Datasheets for parts already onboarded live in `<lib_dir>/datasheets/<MPN>/`.
