# kicad-tools

Installable Python package (`kicadsync`) implementing a JSON-first KiCad
workflow. This repo is the **toolkit only** — PCB designs never live here.
Projects are scaffolded elsewhere with `kicadsync new <name>` and carry their
own CLAUDE.md.

## Architecture

- [kicadsync/cli.py](kicadsync/cli.py) — `kicadsync` entry point; project
  discovery-by-convention (`<name>.json` + `kicad/<name>.kicad_sch`), the
  `new` scaffolder, and thin dispatch to the modules below.
- [kicadsync/json_schem_sync.py](kicadsync/json_schem_sync.py) — the core:
  generate/push/pull between design JSON and `.kicad_sch`, plus `list-libs`
  symbol scanning. Large and battle-tested; prefer minimal, surgical edits.
- [kicadsync/kicad_sch_sexp.py](kicadsync/kicad_sch_sexp.py),
  [kicad_sch_model.py](kicadsync/kicad_sch_model.py),
  [kicad_sch_ops.py](kicadsync/kicad_sch_ops.py) — native KiCad 9
  S-expression parser/model/operations. Unknown blocks are preserved on
  round-trip; keep that invariant.
- [kicadsync/kicad_pcb_model.py](kicadsync/kicad_pcb_model.py) — board-side
  sibling: footprints, pads+nets, courtyard rect decomposition, position
  writes (same round-trip invariant). `set_at` must maintain two KiCad
  conventions: pad/text angles are stored ABSOLUTE, and an embedded
  `(zone ...)` (antenna keep-outs) stores its polygon in ABSOLUTE BOARD
  coordinates — it has to be re-derived or the zone is left behind.
- [kicadsync/pcb_place.py](kicadsync/pcb_place.py) — `kicadsync place`:
  clusters footprints by the design JSON's comment groups and places
  satellites at net-connected anchor pins. No JSON schema additions — intent
  is inferred from structure + connectivity only. Also draws each group's
  labelled box on `Cmts.User`; those nodes carry the `c0de9ace-` UUID prefix
  so a re-run replaces exactly its own annotations and nothing else.
- Schematic pin artifacts (net labels, stub wires, no-connects) placed by
  generate/push carry deterministic ownership UUIDs sharing the `c0de9ace-`
  prefix with the PCB annotations (`owned_artifact_uuid` in kicad_sch_ops).
  Push claims each symbol's artifacts by UUID and sweeps unclaimed ones, so
  geometry drift can't strand labels (stacked duplicates / silent net
  merges). Unowned labels are user data — never delete them by position.
  `remove_orphan_stub_wires` may only remove wires whose removal is provably
  netlist-neutral.
- [kicadsync/jlc_price_report.py](kicadsync/jlc_price_report.py) — BOM
  price/stock report from the local JLC catalog cache.
- [kicadsync/jlc_catalog.py](kicadsync/jlc_catalog.py) — downloads the
  jlcparts static catalog into the cache.
- [kicadsync/paths.py](kicadsync/paths.py) — ALL machine-local path
  resolution (env var > `~/.config/kicadsync/config.json` > default). Never
  hardcode absolute paths anywhere else; this repo is public.
- [kicadsync/templates/](kicadsync/templates/) — files stamped out by
  `kicadsync new` (`{{NAME}}` substitution), including the per-project
  CLAUDE.md and the two part-onboarding agents.
- [schema/design.schema.json](schema/design.schema.json) — JSON Schema for
  design files. Keep in sync with what json_schem_sync actually reads.
- [parts-library/](parts-library/) — the user's growing parts library
  (footprints as `<MPN>.kicad_mod`, 3D models, gitignored datasheets).
  Committed parts are curated/validated — don't regenerate them casually.

## Rules

- This repo is shared publicly on GitHub: no user-specific paths, no personal
  data, no committed caches or datasheets.
- Caches live in `~/.cache/kicadsync/` (`libs.json`, `jlc/`); the parts
  library location comes from `kicadsync config lib_dir` (normally
  `parts-library/` in this repo — never hardcode that path in code).
- Run tests with `uv run python -m unittest discover tests`. The suite
  shells out to `kicad-cli` when KiCad is installed; those assertions are
  skipped otherwise.
- Design-JSON semantics (NC pins, DNP, test points, sheet instancing) are
  documented in [README.md](README.md) and the template CLAUDE.md — update
  both when behavior changes.
