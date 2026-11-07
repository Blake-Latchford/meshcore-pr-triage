# MeshCore PR & Issue Triage

Analyzes every open PR and issue on [meshcore-dev/MeshCore](https://github.com/meshcore-dev/MeshCore) and produces:

- feedback for submitters on why a PR might be getting overlooked (failing CI, merge conflicts, no description, no testing mentioned, etc.)
- a reviewer-priority score for maintainers, favoring small/clean/well-tested/engaged PRs over large or stale ones
- feedback for issue reporters (bug reports missing repro steps/board/firmware version, reported against old firmware, never answered, about to be auto-closed by the stale bot)
- an issue-priority score for maintainers, favoring issues with broad interest, independent confirmation, or a fix already pending in an open PR

## Usage

Requires the [`gh` CLI](https://cli.github.com/) authenticated, and Python 3.11+ with `requests` installed.

```
python3 pr_triage.py
```

Writes to `./out/`:

- `prs.json` — full computed data per PR
- `issues.json` — full computed data per issue
- `report.md` — markdown summary (top review candidates, likely-abandoned PRs, top issues, stale issues worth rescuing, issues with a fix pending, never-answered issues)
- `dashboard.html` — a sortable/filterable dashboard with PR and issue tabs; open directly in a browser (append `#issues` to open on the issues tab)

Options: `--repo owner/name` (default `meshcore-dev/MeshCore`), `--skip-extras` (skip per-PR merge/author/issue-link lookups and per-author merge-history counts, faster but less data — also means issues won't be linked to the PRs that address them), `--skip-issues` (PRs only), `--out-dir`.

## Scoring

### PRs

See the comments in `pr_triage.py` for the full breakdown of what's rewarded and penalized. Broadly: an author with previously merged PRs (log-scaled by how many), small diffs, passing CI, no conflicts, and engagement from established contributors raise a PR's priority; drafts, failing CI, conflicts, huge diffs, and long descriptions lower it.

The author-history, diff-size, and description-length weights were fit against every PR to `dev` from March 2025 to September 2026 (merged vs. closed or still open). On that history, mentioning testing, linking an issue, and having a non-empty description made no difference to whether a PR got merged once those three were accounted for, so they're shown as information but not scored. The fitted weights rank historical outcomes at AUC 0.81, vs. 0.64 for the previous hand-picked ones.

To re-check the weights later, run `python3 backtest.py` (add `--grid` to search for better ones, `--refetch` to update the cached PR history in `out/backtest_prs.json`). It scores the historical PRs with the tool's own scoring functions and reports the AUC and merge rates by factor; see its docstring for what it can and can't reconstruct.

A description that's disproportionately structured for the size of the change is also flagged separately for maintainers to eyeball (it can look templated, but so does a genuinely thorough security writeup); that flag isn't scored beyond the plain length penalty.

### Issues

See `analyze_issue` in `pr_triage.py`. Issues are classified (bug / feature / hardware / question / other) from labels and title, since the repo has no issue templates. Distinct participants and reactions, other users confirming a bug, a linked open PR, and never having had a response raise priority; long idle time, very short descriptions, and bug reports missing what CONTRIBUTING.md asks for (repro steps, board, firmware version) or filed against firmware two or more minor releases behind lower it. Idle time is measured from the last human comment, because the stale bot's own comment bumps `updatedAt`.

The repo's stale bot closes issues 7 days after labelling them `stale` unless they're `keep-open` or someone comments. The report's "About to be auto-closed, but people care" section lists stale issues with real interest, so a maintainer can decide deliberately before the bot does.
