# MeshCore PR Triage

Analyzes every open PR on [meshcore-dev/MeshCore](https://github.com/meshcore-dev/MeshCore) and produces:

- feedback for submitters on why a PR might be getting overlooked (failing CI, merge conflicts, no description, no testing mentioned, etc.)
- a reviewer-priority score for maintainers, favoring small/clean/well-tested/engaged PRs over large or stale ones

## Usage

Requires the [`gh` CLI](https://cli.github.com/) authenticated, and Python 3.11+ with `requests` installed.

```
python3 pr_triage.py
```

Writes to `./out/`:

- `prs.json` — full computed data per PR
- `report.md` — markdown summary (top review candidates, likely-abandoned list)
- `dashboard.html` — a sortable/filterable dashboard; open directly in a browser

Options: `--repo owner/name` (default `meshcore-dev/MeshCore`), `--skip-extras` (skip per-PR merge/author/issue-link lookups, faster but less data), `--out-dir`.

## Scoring

See the comments in `pr_triage.py` for the full breakdown of what's rewarded and penalized. Broadly: small diffs, passing CI, no conflicts, a real description, mentioned testing, established-contributor engagement, and linked issues all raise a PR's priority; drafts, failing/incomplete CI, conflicts, and huge diffs lower it. A description that's disproportionately long/structured for the size of the change is flagged for maintainers to eyeball, but never scored — that pattern is indistinguishable from a genuinely thorough writeup (e.g. a security analysis) by structure alone.
