# parts-library

The personal KiCad parts library that grows alongside the tools — one part at a
time as components are onboarded (usually by the `kicad-part-builder` agent).
If you fork this repo, this folder becomes *your* library; the shipped parts
are a seed/example set.

## Layout

| Folder | Contents | Naming |
| --- | --- | --- |
| `footprints.pretty/` | Custom footprints | `<MPN>.kicad_mod` |
| `3dshapes/` | 3D models referenced by footprints | `<name>.step` / `.wrl` |
| `symbols/` | Custom symbols (optional) | `<lib>.kicad_sym` |
| `datasheets/<MPN>/` | Datasheet PDF + footprint-page PNG renders | `<MPN>.pdf`, `<MPN>-<page>.png` |

`datasheets/` is gitignored — the PDFs are copyrighted manufacturer documents
and shouldn't be redistributed. They are a local cache; the agents re-fetch
them on demand.

## One-time KiCad setup

Tell kicadsync where the library is:

```bash
kicadsync config lib_dir /path/to/kicad-tools/parts-library
```

In KiCad → Preferences → Configure Paths, add:

| Variable | Value |
| --- | --- |
| `KICADSYNC_LIB` | absolute path to this folder |
| `MY_3D_MODELS` | `${KICADSYNC_LIB}/3dshapes` |

Then in Preferences → Manage Footprint Libraries, add a global library:

| Nickname | Library Path |
| --- | --- |
| `parts` | `${KICADSYNC_LIB}/footprints.pretty` |

Projects scaffolded by `kicadsync new` reference the library the same way, so
their `fp-lib-table` files stay machine-independent. Design JSON refers to
custom footprints as `parts:<MPN>`.
