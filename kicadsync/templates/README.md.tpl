# {{NAME}}

PCB project. The schematic source of truth is [`{{NAME}}.json`](./{{NAME}}.json);
KiCad files in [`kicad/`](./kicad/) are generated/updated from it with
[kicadsync](https://github.com/otaviogood/kicad-tools).

## Workflow

```bash
# after editing {{NAME}}.json:
kicadsync push          # updates kicad/{{NAME}}.kicad_sch (generates it on first
                        # run), sweeps stale labels, and health-checks the netlist
kicadsync jlc-report {{NAME}}.json   # JLC price/stock check for the BOM
kicadsync pull          # optional: read net-label edits made in KiCad back into JSON
kicadsync check         # optional: re-run the health check after manual KiCad edits
```

Then open `kicad/{{NAME}}.kicad_pro` in KiCad to review the schematic, update
the PCB from the schematic, and route the board.

## Layout

- `{{NAME}}.json` — the design: components, pins, nets (schema in kicad-tools)
- `kicad/` — KiCad project (schematic is generated; PCB routing is hand-edited)
- `docs/` — design notes, architecture, calculations
- `firmware/` — code that runs on the board
