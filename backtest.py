#!/usr/bin/env python3
"""Check pr_triage.py's PR scoring weights against what actually got merged.

Fetches every PR opened against the base branch (default dev) since --since,
merged, closed, or still open, and scores each with the parts of
pr_triage.analyze() that can be reconstructed after the fact: the author's
merge history as of when the PR was opened, diff size, description length,
draft status, and the agent marker. CI, merge conflicts, review state,
engagement, and age can't be (they describe the PR's state today, not when
a maintainer was deciding), so they're left out on both sides of the
comparison.

Reports how well that score ranks merged PRs above unmerged ones (AUC: 0.5 is
a coin flip, 1.0 is perfect), merge rates by factor, and with --grid, the
best-ranking weights from a small search next to the current ones. Still-open
PRs count as not merged, since most PRs that don't get merged are never
closed either; PRs younger than --min-age-days are left out because they
haven't had time to go either way.

Author merge history only counts merges into the base branch since --since,
whereas pr_triage.py counts all of an author's merges, so long-time
contributors look newer here than they do in the live score.
"""
import argparse
import bisect
import collections
import itertools
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pr_triage
from pr_triage import (description_length_score, describe_body, diff_size_score,
                       prior_merge_score, run_gh)

FIELDS = "number,title,author,createdAt,closedAt,mergedAt,additions,deletions,isDraft,body,mergedBy"
WINDOW_DAYS = 30
# gh's --limit per window; a window that comes back this full may have been
# truncated, so it's warned about rather than silently undercounted.
WINDOW_LIMIT = 1000


def fetch_all(repo, base, since):
    prs = {}
    start, end = since, date.today() + timedelta(days=1)
    while start < end:
        stop = min(start + timedelta(days=WINDOW_DAYS), end)
        print(f"  fetching {start}..{stop}...", file=sys.stderr)
        out = run_gh([
            "pr", "list", "--repo", repo, "--base", base, "--state", "all",
            "--search", f"created:{start}..{stop - timedelta(days=1)}",
            "--limit", str(WINDOW_LIMIT), "--json", FIELDS,
        ])
        window = json.loads(out)
        if len(window) >= WINDOW_LIMIT:
            print(f"  WARNING: {start}..{stop} hit the {WINDOW_LIMIT} limit and may be truncated", file=sys.stderr)
        for pr in window:
            prs[pr["number"]] = pr
        start = stop
    return list(prs.values())


def build_rows(prs, min_age_days):
    maintainers = {pr["mergedBy"]["login"] for pr in prs if pr.get("mergedBy")}
    merge_times = collections.defaultdict(list)
    for pr in prs:
        if pr["mergedAt"]:
            merge_times[pr["author"]["login"]].append(pr["mergedAt"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    rows = []
    for pr in prs:
        if pr["createdAt"] >= cutoff:
            continue
        author = pr["author"]["login"]
        rows.append({
            "merged": bool(pr["mergedAt"]),
            "maintainer": author in maintainers,
            # ISO-8601 UTC timestamps compare correctly as strings.
            "priorMerged": sum(t < pr["createdAt"] for t in merge_times[author]),
            "diffLines": pr["additions"] + pr["deletions"],
            "descLength": describe_body(pr["body"])[1],
            "isDraft": pr["isDraft"],
            "agentMarker": "\U0001F916" in pr["title"],
        })
    return rows


def score(row):
    # Mirrors the matching terms in pr_triage.analyze().
    s = prior_merge_score(row["priorMerged"]) + diff_size_score(row["diffLines"]) \
        + description_length_score(row["descLength"])
    if row["isDraft"]:
        s -= 100
    if row["agentMarker"]:
        s -= 60
    return s


def auc(rows):
    """Probability a random merged PR outscores a random unmerged one (ties count half)."""
    merged = [score(r) for r in rows if r["merged"]]
    unmerged = sorted(score(r) for r in rows if not r["merged"])
    if not merged or not unmerged:
        return float("nan")
    wins = sum(bisect.bisect_left(unmerged, s) + (bisect.bisect_right(unmerged, s) - bisect.bisect_left(unmerged, s)) / 2
               for s in merged)
    return wins / (len(merged) * len(unmerged))


def bucket(value, edges):
    """(sort index, label) so tables print in numeric order, not string order."""
    for i, edge in enumerate(edges):
        if value <= edge:
            return i, f"<={edge}"
    return len(edges), f">{edges[-1]}"


def merge_rate_table(rows, label, key):
    groups = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r["merged"])
    print(f"\n{label}")
    for k in sorted(groups):
        v = groups[k]
        name = k[1] if isinstance(k, tuple) else k
        print(f"  {str(name):12} {sum(v) / len(v):5.0%}  (n={len(v)})")


# Weight constants searched by --grid, with the values tried for each.
GRID = {
    "PRIOR_MERGE_LOG_WEIGHT": [6, 9, 12, 15],
    "PRIOR_MERGE_BONUS_MAX": [30, 40, 50, 60],
    "LONG_DESC_CHARS": [200, 300, 500, 800],
    "LONG_DESC_LOG_WEIGHT": [0, 6, 12, 18],
    "LONG_DESC_PENALTY_MAX": [20, 30, 40],
}


def grid_search(rows):
    current = {name: getattr(pr_triage, name) for name in GRID}
    results = []
    for values in itertools.product(*GRID.values()):
        for name, value in zip(GRID, values):
            setattr(pr_triage, name, value)  # the score functions read these module globals at call time
        results.append((auc(rows), dict(zip(GRID, values))))
    for name, value in current.items():
        setattr(pr_triage, name, value)
    results.sort(key=lambda r: r[0], reverse=True)
    print(f"\nGrid search ({len(results)} combinations), top 5 by AUC:")
    for a, weights in results[:5]:
        print(f"  {a:.3f}  {weights}")
    print(f"  current: {auc(rows):.3f}  {current}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=pr_triage.REPO)
    parser.add_argument("--base", default="dev")
    parser.add_argument("--since", type=date.fromisoformat, default=date(2025, 3, 1))
    parser.add_argument("--min-age-days", type=int, default=30)
    parser.add_argument("--cache", default=str(Path(__file__).parent / "out" / "backtest_prs.json"),
                        help="Fetched PRs are saved here and reused on later runs; delete it to refetch")
    parser.add_argument("--refetch", action="store_true", help="Ignore --cache and fetch fresh data")
    parser.add_argument("--grid", action="store_true", help="Also search for better-ranking weights")
    args = parser.parse_args()

    cache = Path(args.cache)
    if cache.exists() and not args.refetch:
        print(f"Using cached PRs from {cache} (--refetch to update)", file=sys.stderr)
        prs = json.loads(cache.read_text())
    else:
        print(f"Fetching PRs against {args.base} since {args.since} from {args.repo}...", file=sys.stderr)
        prs = fetch_all(args.repo, args.base, args.since)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(prs))

    rows = build_rows(prs, args.min_age_days)
    contributors = [r for r in rows if not r["maintainer"]]
    merged = sum(r["merged"] for r in rows)
    print(f"\n{len(rows)} PRs older than {args.min_age_days} days, {merged} merged ({merged / len(rows):.0%})")
    print(f"AUC, all PRs:             {auc(rows):.3f}")
    print(f"AUC, non-maintainer PRs:  {auc(contributors):.3f}")

    print("\nMerge rates, non-maintainer PRs:")
    merge_rate_table(contributors, "Author's prior merged PRs", lambda r: bucket(r["priorMerged"], [0, 2, 9]))
    merge_rate_table(contributors, "Diff lines", lambda r: bucket(r["diffLines"], [10, 50, 200, 1000]))
    merge_rate_table(contributors, "Description length (cleaned chars)",
                     lambda r: bucket(r["descLength"], [0, 200, 1000, 3000]))
    merge_rate_table(contributors, "Draft", lambda r: r["isDraft"])

    if args.grid:
        grid_search(rows)


if __name__ == "__main__":
    main()
