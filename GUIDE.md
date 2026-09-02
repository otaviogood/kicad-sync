# Designing a PCB with kicad-tools, step by step

This guide walks you from an empty folder to a routed, orderable board. Every
command shown here was run for real; the example project is a small USB-C
powered LED badge with two identical LED channels.

The mental model in one paragraph: **your design lives in one JSON file.**
Components, pin connections, net names — all of it. The `kicadsync` CLI turns
that JSON into a real KiCad schematic and keeps the two in sync. You (or your
AI assistant) iterate on the JSON; KiCad is for *reviewing* the schematic and
*routing* the board. You never draw a schematic by hand.

```
 you / AI edit     kicadsync push (+ health check)     KiCad (human)
 ┌────────────┐  ──────────────────────────────▶  ┌──────────────────┐
 │ board.json │                                   │ board.kicad_sch  │──▶ PCB layout
 └────────────┘  ◀──────────────────────────────  └──────────────────┘    gerbers
                        kicadsync pull
```

Every `push` ends with a built-in health check — it lints the JSON for
suspicious nets and audits a real KiCad netlist export against the design —
so a clean push exit means the schematic really matches your JSON.

---

## Step 0 — One-time setup

Do this once per machine, not per project.

```bash
git clone https://github.com/otaviogood/kicad-tools
uv tool install --editable ./kicad-tools     # puts `kicadsync` on your PATH

# Tell the tools where the parts library is (it ships inside this repo):
kicadsync config lib_dir /path/to/kicad-tools/parts-library

# Cache symbol metadata from your installed KiCad libraries (~1 min, 22k symbols).
# Generation uses this to give known parts real symbols instead of placeholders.
kicadsync list-libs

# Optional but recommended (~2 GB, enables offline price/stock reports):
kicadsync jlc fetch-all
```

And in KiCad itself (Preferences → Configure Paths):

| Variable | Value |
| --- | --- |
| `KICADSYNC_LIB` | absolute path to `kicad-tools/parts-library` |
| `MY_3D_MODELS` | `${KICADSYNC_LIB}/3dshapes` |

Sanity check: `kicadsync config` should show `lib_dir` set and both caches
`present`.

---

## Step 1 — Start a project

```bash
cd ~/Documents/devm1/pcb        # or wherever your boards live
kicadsync new led-badge
cd led-badge
```

You get a git-initialized folder:

```
led-badge/
├── led-badge.json      ← THE DESIGN. Everything else derives from this.
├── kicad/              ← KiCad project (schematic will be generated into here)
│   ├── led-badge.kicad_pro
│   └── fp-lib-table    ← wires up the `parts` library via ${KICADSYNC_LIB}
├── CLAUDE.md           ← teaches an AI assistant this whole workflow + schema
├── .claude/agents/     ← datasheet fetcher + footprint builder agents
├── docs/  firmware/  README.md  .gitignore
```

Project names must be `letters/digits/._-` — they become file and KiCad
project names.

---

## Step 2 — Describe the circuit in JSON

Open `led-badge.json`. It starts with a placeholder test point; replace it
with your circuit. One component looks like this:

```jsonc
{
  "refdes": "R_CC1",
  "mpn": "0603WAF5101T5E",          // manufacturer part number
  "LCSC": "C23186",                 // JLCPCB catalog number (goes WITH mpn)
  "footprint": "Resistor_SMD:R_0603_1608Metric",
  "description": "5.1k CC1 pulldown",
  "notes": "USB-C sink advertisement.",
  "connections": [
    { "number": "1", "name": "1", "net": "CC1" },
    { "number": "2", "name": "2", "net": "GND" }
  ]
}
```

The rules that matter:

- **`connections[].number` is sacred.** It must match the physical pin/pad
  numbering exactly, including alphanumerics (`A1`, `B5`, `EP`). Get it from
  the datasheet pinout table. Everything downstream — schematic, netlist,
  footprint pads — trusts these numbers.
- **Nets are just names.** Two pins with `"net": "CC1"` are connected. A typo
  makes a silent disconnection — `push` flags these as `SINGLE-PIN net`
  findings in its health check (Step 5).
- `mpn` and `LCSC` come as a pair (both or neither). Parts with no `mpn`
  (test points, mounting holes) stay off the BOM but keep their footprint.
- Special pins: no-connect is `"net": "__NC__"`; DNP parts get `"dnp": true`.
- Semantic refdes names (`U_MCU`, `R_CC1`, `J_USB`, `TP_GND_1`) keep the JSON
  readable; KiCad annotation (R1, R2…) is handled automatically.
- `//` comments are fine — design notes belong in the file. (But see the
  `pull` warning in Step 6 before you invest in beautiful comments.)

Full schema: [`schema/design.schema.json`](schema/design.schema.json).

### Repeated circuitry: sheets

For N copies of the same sub-circuit (LED channels, motor drivers, array
elements), define it once and instance it:

```jsonc
"sheet_definitions": [
  {
    "sheet_id": "led_channel",
    "file": "led_channel.kicad_sch",
    "components": [
      { "refdes": "R_LIM", "...": "...",
        "connections": [
          { "number": "1", "name": "1", "net": "CH_IN", "scope": "hierarchical" },
          { "number": "2", "name": "2", "net": "LED_A", "scope": "local" }
        ] },
      { "refdes": "D_LED", "...": "..." }
    ]
  }
],
"sheet_instances": [
  { "instance_name": "LED_CH_A", "sheet_id": "led_channel",
    "pin_map": [ { "name": "CH_IN", "net": "LED_EN" } ] },
  { "instance_name": "LED_CH_B", "sheet_id": "led_channel",
    "pin_map": [ { "name": "CH_IN", "net": "LED_EN" } ] }
]
```

Net **scope** is what makes this work:

| scope | meaning | in the netlist |
| --- | --- | --- |
| `global` (default) | same net everywhere, crosses sheets | `LED_EN`, `GND` |
| `local` | private to each sheet instance | `/LED_CH_A/LED_A` vs `/LED_CH_B/LED_A` |
| `hierarchical` | interface pin of the sheet; parent connects it via `pin_map` | dissolves into the parent net |

So above: each channel has its own private `LED_A` node, both channels'
`CH_IN` inputs are driven by the shared `LED_EN` net, and `GND` is global.

---

## Step 3 — Pick real, orderable parts

Work with the JLC catalog from the start so you never design in a part that's
out of stock:

```bash
kicadsync jlc search-mpn KT-0603R        # find LCSC number, price, stock
kicadsync jlc-report led-badge.json      # whole-BOM price/stock report
```

`jlc-report` shows two sorted views (highest unit price, lowest stock) plus a
**Missing From JLC Cache** section — parts there either have a typo'd
MPN/LCSC or aren't stocked; fix them now, not at order time. Prefer parts
marked `Basic` (no setup fee at JLC assembly).

For a part that needs a datasheet dive or a custom footprint, use the agents
that came with the project (in Claude Code):

1. **`datasheet-footprint-pages`** — fetches the official datasheet into the
   parts library and renders the package/land-pattern pages to PNG.
2. **`kicad-part-builder`** — downloads or generates a validated
   `<MPN>.kicad_mod` in `parts-library/footprints.pretty/`, cross-checking pad
   numbers against your design JSON.

Then reference it as `"footprint": "parts:<MPN>"`.

---

## Step 4 — Generate the schematic

```bash
kicadsync push
```

From inside the project folder, no arguments needed — it finds
`led-badge.json` and `kicad/led-badge.kicad_sch` by convention. The first run
generates the schematic (root sheet + one file per sheet definition); later
runs update in place.

Example first-run output:

```
[push] kicad/led-badge.kicad_sch does not exist yet — running generate instead
[generate] Loaded 22470 symbols from ~/.cache/kicadsync/libs.json
[generate] Root components: 4
[generate] Child definitions: 1
[generate] Sheet instances: 2
[check] OK: 8 nets match the design JSON
```

That last `[check]` line is the built-in health check — Step 5 explains what
it verifies and what its findings mean.

Open `kicad/led-badge.kicad_pro` in KiCad and look at the schematic:

- Parts whose MPN matched the symbol cache get real library symbols.
- Unmatched parts get auto-drawn placeholder symbols marked **`NOLIB`** with a
  thick border. They're electrically correct (right pin count/numbers/nets) —
  the marker is a *review cue*, not an error. Check their pinout extra
  carefully against the datasheet.

---

## Step 5 — Read the health check (it runs on every push)

Every `push` (and `generate`) ends with an automatic health check: a
single-pin-net lint on your JSON, then a netlist exported through real KiCad
(`kicad-cli`) and audited against the design. A clean run ends with:

```
[check] OK: 8 nets match the design JSON
```

and push exits 0. Findings look like this, and make push exit 1 (the
schematic is still written — the exit code is your signal to fix and re-push):

```
[check] SINGLE-PIN net: LED_ENABLE (typo in a net name?)   ← net with one connection
[check] STALE net in schematic, not in JSON: LED_EN        ← leftover label (rare; re-push)
[check] UNWIRED net in JSON, missing from schematic: CC2   ← labels deleted in KiCad
```

Single-pin nets are the classic JSON typo: `"LED_EN"` on one pin, `"LED_ENA"`
on the other creates two half-nets and no error anywhere else. During this
guide's dry run, the check caught a real missing CC2 pulldown exactly this
way. The lint part runs even without KiCad installed; if `kicad-cli` is
missing, push notes that the netlist audit was skipped and still exits 0 when
the lint is clean.

The standalone `kicadsync check` command re-runs the same health check
without pushing — useful after you delete or move labels by hand in KiCad.

Also run KiCad's ERC (Inspect → Electrical Rules Checker) before layout —
the health check verifies JSON↔schematic agreement, ERC verifies electrical
sanity.

---

## Step 6 — Iterate

The loop is: **edit JSON → `push` → glance at KiCad**. Push is incremental —
it updates properties and adds missing labels/wires without destroying
schematic cleanup you've done by hand in KiCad. Renaming or deleting a net in
the JSON is handled too: push places the new labels and sweeps any label
whose net name no longer exists in the JSON, even ones you've dragged around
during manual cleanup (your wires and label positions for live nets are never
touched).

Two gotchas to know:

1. **`generate` refuses to overwrite** an existing schematic without
   `--force`, because a rebuild discards manual edits. `push` is the everyday
   command; `generate --force` is for when you want a fresh auto-layout and
   the schematic has no precious hand edits yet.
2. **`pull` rewrites your JSON file.** If you rename nets inside KiCad,
   `kicadsync pull` brings them back into the JSON — but it re-serializes the
   file: comments and custom formatting are lost (it prints a warning). Use
   `kicadsync pull --out-json /tmp/pulled.json` and diff if you want to
   review first. Day to day it's simpler to treat the JSON as the only place
   nets get renamed — and if you do rename in KiCad, `pull` before your next
   `push`, or the sweep will treat the KiCad-only name as stale.

---

## Step 7 — Layout

1. Open the board editor, **Tools → Update PCB from Schematic** (F8), save.
2. `kicadsync place` — turns the imported heap into a starting arrangement:
   one cluster per `// ----` comment group in your JSON, passives sitting at
   the IC/connector pins their nets actually connect to, and clusters that
   share nets placed next to each other. Each group is outlined and labelled
   on the `User.Comments` layer so the structure is visible at a glance.
3. Refine by hand. **Lock** (hover + `L`) anything you've finalized — `place`
   never moves a locked footprint, and re-flows its group around it. Unlocked
   anchors keep any rotation you gave them.
4. Re-run `place` freely (after JSON changes, or to try a different `--gap`).
   It's deterministic, and it replaces its own group boxes rather than piling
   up new ones. `--dry-run` reports the plan without writing; `--no-outlines`
   skips (and clears) the boxes.
5. Draw the outline on Edge.Cuts — once it exists, `place` packs groups inside
   it instead of an auto-sized area. Then route and pour zones.
6. Run DRC.

Placement is a starting point, not a finished layout: it optimises "related
parts are near each other", not thermals, EMI, or mechanical constraints.

Apart from `place` (which only rewrites footprint positions and its own
group boxes), the `.kicad_pcb` is yours — routing, zones, and outlines are
never touched. Re-running `push` after layout is safe: footprint/value
updates flow in via F8 again.

If you added test points or DNP'd a part in JSON (`"dnp": true`), F8 carries
that through — DNP parts keep pads on the board but drop out of the BOM.

---

## Step 8 — Order it

```bash
kicadsync jlc-report led-badge.json      # final cost + stock sanity check
```

Then export fabrication files from KiCad (File → Fabrication Outputs, or the
[Fabrication Toolkit](https://github.com/bennymeg/JLC-Plugin-for-KiCad)
plugin, which emits JLC-ready gerbers/BOM/CPL in one click — the BOM picks up
the `LCSC` field that the sync wrote into every symbol) and upload to
JLCPCB. Commit the project repo; it's self-contained and shareable.

---

## Command cheat sheet

| Situation | Command |
| --- | --- |
| New board | `kicadsync new <name>` |
| JSON changed (incl. net renames) | `kicadsync push` (health check runs automatically) |
| Renamed nets in KiCad | `kicadsync pull` (rewrites JSON; comments lost) |
| Deleted/moved labels by hand in KiCad | `kicadsync check` (re-audit without pushing) |
| Want a fresh auto-layout | `kicadsync generate --force` (discards manual schematic edits) |
| Footprints are a heap after F8 | `kicadsync place` (cluster + place by group and connectivity) |
| Groups too tight / too loose | `kicadsync place --gap 6` (default 4mm) |
| Keep a part where I put it | Lock it in KiCad (hover + `L`), then re-run `place` |
| What's this part cost? | `kicadsync jlc search-mpn <MPN>` |
| Whole-BOM cost/stock | `kicadsync jlc-report <design.json>` |
| Placeholders for known parts? | `kicadsync list-libs` (refresh symbol cache) |
| Where's my parts library? | `kicadsync config` |

## Troubleshooting

- **Every part is a NOLIB placeholder** — the symbol cache is missing: run
  `kicadsync list-libs` (check `kicadsync config` shows `libs.json: present`).
- **`jlc-report` says everything is missing** — the catalog cache is missing:
  `kicadsync jlc fetch-all` (~2 GB, one time).
- **Push says "skipping netlist audit"** — `kicad-cli` not found; install
  KiCad or put `kicad-cli` on PATH. The JSON lint still runs; the full audit
  resumes once KiCad is available (standalone `kicadsync check` exits 2 in
  this state).
- **Custom footprint not found in KiCad** — the `KICADSYNC_LIB` path variable
  isn't set in KiCad's Configure Paths, or `kicadsync config lib_dir` was
  never run (scaffolded `fp-lib-table` references `${KICADSYNC_LIB}`).
- **Two nets where you expected one** — a `SINGLE-PIN net` finding almost
  always means a net-name typo in the JSON. If the JSON is right, someone
  edited labels in KiCad — run `kicadsync check` to see what disagrees, and
  re-push.
