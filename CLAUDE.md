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
