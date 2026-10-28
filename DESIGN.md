# Genesis Blackbox Local Inspector: Design Specification

## Design Read
- **Audience:** Backend engineers and AI-assisted developers inspecting SQLite WAL execution traces and crash autopsies on `localhost`.
- **Job to be done:** Scan high-density chronological telemetry (HTTP, SQL, subprocess, unhandled exceptions) alongside a copy-ready `.blackbox.md` causal autopsy without leaving a local browser tab.
- **Theme Strategy:** Defaults to a dark developer terminal palette (`data-theme="dark"`) suited for side-by-side IDE debugging, paired with an explicit, persistent Light/Dark mode toggle (`localStorage["blackbox-theme"]` + `prefers-color-scheme` fallback).
- **Dials:**
  - `Variance: 3` (predictable asymmetric 3:2 split at desktop, single-column stacked workflow below `1024px`).
  - `Motion: 2` (instant state updates on 3s polling; subtle 120ms focus/hover transitions; respects `prefers-reduced-motion`).
  - `Density: 7` (compact monospace tabular telemetry paired with 44px minimum interactive control hit areas).

## Verified Token Palette (WCAG AA Passed via `contrast-check.py`)

### Dark Theme (`:root`, `[data-theme="dark"]`)
- `--bg`: `#0d1117`
- `--surface`: `#151b23`
- `--code-bg`: `#090d13`
- `--border`: `#30363d`
- `--text`: `#e6edf3` (`16.02:1` on `--bg`, `14.65:1` on `--surface`)
- `--text-muted`: `#9da7b3` (`7.76:1` on `--bg`, `7.10:1` on `--surface`)
- `--accent`: `#79c0ff` (`8.90:1` on `--surface`)
- `--danger-text`: `#ffa198` on `--danger-bg`: `#2d1215` (`8.94:1`)
- `--ok-text`: `#56d364` on `--ok-bg`: `#0f2419` (`8.47:1`)

### Light Theme (`[data-theme="light"]`)
- `--bg`: `#f6f8fa`
- `--surface`: `#ffffff`
- `--code-bg`: `#f0f3f6`
- `--border`: `#d0d7de`
- `--text`: `#1f2328` (`14.84:1` on `--bg`, `15.80:1` on `--surface`)
- `--text-muted`: `#57606a` (`6.00:1` on `--bg`, `6.39:1` on `--surface`)
- `--accent`: `#0969da` (`5.19:1` on `--surface`)
- `--danger-text`: `#a40e26` on `--danger-bg`: `#ffebe9` (`6.86:1`)
- `--ok-text`: `#116329` on `--ok-bg`: `#dafbe1` (`6.64:1`)
