#!/usr/bin/env python3
"""PR triage tool for meshcore-dev/MeshCore.

Fetches all open PRs, flags likely reasons each one is stalled (for
feedback to the submitter), and computes a reviewer-priority score (for
maintainers deciding what to look at next). Requires the `gh` CLI to be
authenticated.

Outputs, under --out-dir (default ./out):
  prs.json        - full computed data per PR
  report.md       - terminal/markdown summary (top review candidates,
                    likely-abandoned list)
  dashboard.html  - static, self-contained, sortable/filterable table;
                    open directly in a browser, no server needed
"""
import argparse
import concurrent.futures
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO = "meshcore-dev/MeshCore"
GRAPHQL_URL = "https://api.github.com/graphql"

PR_EXTRAS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      mergeable
      mergeStateStatus
      authorAssociation
      comments(last: 50) {
        nodes {
          authorAssociation
          author { login }
        }
      }
      closingIssuesReferences(first: 5) {
        nodes { number }
      }
      reactionGroups {
        users(first: 20) { nodes { login } }
      }
      timelineItems(first: 10, itemTypes: [CROSS_REFERENCED_EVENT]) {
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on Issue { number }
            }
          }
        }
      }
    }
  }
}
"""

# GitHub's authorAssociation values that mean "has an established relationship
# with this repo" (previously-merged contribution, or a maintainer), as
# opposed to a first-time/anonymous drive-by.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}

BULK_FIELDS = ",".join([
    "number", "title", "author", "createdAt", "updatedAt", "additions",
    "deletions", "changedFiles", "isDraft", "labels", "reviewDecision",
    "url", "baseRefName", "body", "reviewRequests", "statusCheckRollup",
])

STALE_DAYS = 21
LARGE_DIFF_LINES = 400
LARGE_DIFF_FILES = 15
GUIDELINE_LINES = 100  # CONTRIBUTING.md's own "< 100 lines" guidance
MIN_DESC_CHARS = 40

# Diff-size score component: log-scaled so a 1-line fix scores far above a
# 1000-line one, not just above/below a couple of fixed cutoffs. The penalty
# floor is deep enough (-70) that it keeps discriminating between "large" and
# "gigantic" diffs instead of flatlining at the same value for anything past
# ~150 lines - otherwise a 500-line PR and a 150,000-line vendored-SDK dump
# score identically on size, and a big PR with every trust/engagement bonus
# stacked on top can out-rank a tiny clean fix despite the size penalty.
DIFF_SIZE_BONUS_MAX = 30
DIFF_SIZE_PENALTY_MAX = -70
DIFF_SIZE_LOG_WEIGHT = 8


def diff_size_score(diff_lines):
    return max(DIFF_SIZE_PENALTY_MAX, DIFF_SIZE_BONUS_MAX - DIFF_SIZE_LOG_WEIGHT * math.log2(diff_lines + 1))


def run_gh(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def fetch_prs(repo, limit):
    out = run_gh([
        "pr", "list", "--repo", repo, "--state", "open", "--limit", str(limit),
        "--json", BULK_FIELDS,
    ])
    return json.loads(out)


def get_github_token():
    return run_gh(["auth", "token"]).strip()


def fetch_pr_extras(repo, numbers):
    """Merge-conflict status, author association, and comment authors all
    need a per-PR call: `gh pr list --json mergeable` times out (HTTP 502)
    once a repo has a few hundred open PRs, because GitHub computes
    mergeability lazily per PR on its backend, and the other two fields
    aren't in `gh pr list --json` at all.

    Queried directly over GraphQL (rather than spawning `gh pr view` per PR)
    so hundreds of lookups share one HTTP connection pool instead of paying
    process-spawn overhead hundreds of times."""
    owner, name = repo.split("/", 1)
    token = get_github_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    # Transport-level failures (5xx, rate limiting, connection resets) get
    # retried with exponential backoff by urllib3 itself: 0.5, 1, 2, 4s.
    retry = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    unknown = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN",
               "authorAssociation": "UNKNOWN", "comments": {"nodes": []},
               "closingIssuesReferences": {"nodes": []}, "timelineItems": {"nodes": []},
               "reactionGroups": []}
    results = {}

    def one(number):
        # GitHub's GraphQL secondary rate limit comes back as HTTP 200 with an
        # errors[].type of RATE_LIMIT, which urllib3's Retry never sees (it
        # only reacts to transport failures and status codes) - retried here
        # by hand, with backoff, since hundreds of concurrent per-PR queries
        # can trip it even while the primary point quota has room to spare.
        for attempt in range(4):
            try:
                resp = session.post(
                    GRAPHQL_URL,
                    json={"query": PR_EXTRAS_QUERY,
                          "variables": {"owner": owner, "name": name, "number": number}},
                    timeout=15,
                )
                resp.raise_for_status()
                payload = resp.json()
                errors = payload.get("errors")
                if errors:
                    if any(e.get("type") == "RATE_LIMIT" for e in errors) and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(str(errors))
                return number, payload["data"]["repository"]["pullRequest"]
            except (requests.RequestException, RuntimeError, KeyError, TypeError):
                if attempt == 3:
                    return number, unknown
        return number, unknown

    with concurrent.futures.ThreadPoolExecutor() as pool:
        for number, data in pool.map(one, numbers):
            results[number] = data
    return results


def days_since(iso_ts, now):
    dt = datetime.fromisoformat(iso_ts)
    return (now - dt).total_seconds() / 86400


def ci_state(rollup):
    if not rollup:
        return "NONE"
    conclusions = [c.get("conclusion") for c in rollup if c.get("__typename") == "CheckRun"]
    states = [c.get("state") for c in rollup if c.get("__typename") == "StatusContext"]
    bad = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}
    if any(c in bad for c in conclusions) or any(s in {"FAILURE", "ERROR"} for s in states):
        return "FAILING"
    pending = [c for c in rollup if c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED"]
    if pending or any(s == "PENDING" for s in states):
        return "PENDING"
    if conclusions or states:
        return "PASSING"
    return "NONE"


def describe_body(body):
    if not body or not body.strip():
        return "EMPTY", 0
    cleaned = re.sub(r"[\[\]xX*\->#`\s]", "", body)
    if len(cleaned) < MIN_DESC_CHARS:
        return "TOO_SHORT", len(cleaned)
    return "OK", len(cleaned)


# Not "AI detection" - that isn't reliably measurable and would unfairly
# brand verbose human writers too. This flags description structure/length
# that's disproportionate to the size of the change (many headers, lots of
# words, a small diff) - but that pattern also fits a genuinely thorough
# security writeup (exact code paths, exploit tables, precise fix), which a
# maintainer should NOT have demoted. So this is surfaced as information
# only, never scored - false positives here are cheap to eyeball and dismiss,
# but wrong if silently baked into the ranking.
PADDING_MIN_HEADERS = 5
PADDING_MAX_DIFF_LINES = 50
PADDING_MIN_WORDS = 200


def is_padded_description(body, diff_lines):
    if not body:
        return False, 0, 0
    headers = len(re.findall(r"(?m)^#{1,3}\s", body))
    words = len(body.split())
    padded = (
        headers >= PADDING_MIN_HEADERS
        and diff_lines <= PADDING_MAX_DIFF_LINES
        and words >= PADDING_MIN_WORDS
    )
    return padded, headers, words


# Heuristic, not exhaustive: firmware changes can't be verified by CI alone
# (it builds/compiles but doesn't flash real hardware), so whether the
# submitter says they tried it on a device is a meaningful independent signal.
TESTING_POSITIVE_RE = re.compile(
    r"\b(tested|testing|verified|confirmed|reproduced|validated|flashed|"
    r"test plan|test steps|ran (?:it |this )?on|works? on|checked on)\b",
    re.IGNORECASE,
)
TESTING_NEGATIVE_RE = re.compile(
    r"\b(not tested|no testing|untested|haven't tested|hasn't been tested|"
    r"did not test|didn't test|couldn't test|unable to test|"
    r"no test(?:ing)? (?:performed|done))\b",
    re.IGNORECASE,
)


def mentions_testing(body):
    if not body:
        return False
    if TESTING_NEGATIVE_RE.search(body):
        return False
    return bool(TESTING_POSITIVE_RE.search(body))


def analyze(pr, extras, now):
    number = pr["number"]
    diff_lines = pr["additions"] + pr["deletions"]
    files = pr["changedFiles"]
    age_days = days_since(pr["createdAt"], now)
    idle_days = days_since(pr["updatedAt"], now)
    ci = ci_state(pr.get("statusCheckRollup"))
    desc_state, desc_len = describe_body(pr.get("body"))
    tested = mentions_testing(pr.get("body"))
    padded, padding_headers, padding_words = is_padded_description(pr.get("body"), diff_lines)
    extra = extras.get(number, {})
    mergeable = extra.get("mergeable", "UNKNOWN")
    review_decision = pr.get("reviewDecision") or "NONE"
    has_reviewer = bool(pr.get("reviewRequests"))
    is_draft = pr["isDraft"]
    title = pr["title"]
    author_login = pr["author"]["login"]
    author_is_contributor = extra.get("authorAssociation") in TRUSTED_ASSOCIATIONS
    comment_nodes = (extra.get("comments") or {}).get("nodes") or []
    has_contributor_comment = any(
        c.get("authorAssociation") in TRUSTED_ASSOCIATIONS
        and c.get("author") and c["author"]["login"] != author_login
        for c in comment_nodes
    )
    links_issue = bool((extra.get("closingIssuesReferences") or {}).get("nodes")) or any(
        (item.get("source") or {}).get("__typename") == "Issue"
        for item in (extra.get("timelineItems") or {}).get("nodes") or []
    )
    # Comments come back oldest-to-newest, so the last node is the most
    # recent activity - if that's the author, the ball is in the reviewer's
    # court rather than the PR waiting on the submitter to respond.
    last_action_by_author = bool(comment_nodes) and comment_nodes[-1].get("author") \
        and comment_nodes[-1]["author"]["login"] == author_login
    # Engagement from anyone but the author, regardless of their trust level -
    # a plain sign that people other than the submitter find this worth a
    # look, separate from has_contributor_comment's trust-weighted version.
    # Counted by unique person, not raw event count, so one user posting many
    # comments (or reacting with several different emoji) can't inflate this
    # the way repeated interactions from a single account otherwise would.
    non_author_comment_users = {
        c["author"]["login"] for c in comment_nodes
        if c.get("author") and c["author"]["login"] != author_login
    }
    non_author_comments = len(non_author_comment_users)
    reaction_users = {
        u["login"]
        for g in extra.get("reactionGroups") or []
        for u in (g.get("users") or {}).get("nodes") or []
        if u and u.get("login") and u["login"] != author_login
    }
    reaction_count = len(reaction_users)
    non_author_activity = non_author_comments + reaction_count
    # Not a merit signal either way - just surfaced so maintainers know a PR
    # is invoking the "auto-merge agent PRs" note in CONTRIBUTING.md, which
    # reads like a prompt-injection attempt aimed at AI coding agents rather
    # than a legitimate project policy.
    suspicious_marker = "\U0001F916" in title

    submitter_flags = []
    if is_draft:
        submitter_flags.append("Marked as a draft — reviewers generally skip drafts until marked ready.")
    if ci == "FAILING":
        submitter_flags.append("CI is failing — reviewers are unlikely to look until checks pass.")
    elif ci == "PENDING":
        submitter_flags.append("CI checks are still running — no impact on priority either way until they finish.")
    elif ci == "NONE":
        submitter_flags.append("No CI checks have run on this PR.")
    if mergeable == "CONFLICTING":
        submitter_flags.append("Has merge conflicts with the base branch and needs a rebase/update.")
    if desc_state == "EMPTY":
        submitter_flags.append("PR description is empty, giving reviewers no context on what changed or why.")
    elif desc_state == "TOO_SHORT":
        submitter_flags.append("PR description is very short, giving reviewers little context on what changed or why.")
    if desc_state == "OK" and not tested:
        submitter_flags.append(
            "Description doesn't mention any testing performed — CI only builds/compiles firmware, "
            "it doesn't flash real hardware, so reviewers have no evidence this was actually tried on a device."
        )
    if diff_lines > LARGE_DIFF_LINES or files > LARGE_DIFF_FILES:
        submitter_flags.append(
            f"Large change ({diff_lines} lines / {files} files) — well over the project's own "
            f"'<{GUIDELINE_LINES} lines' guideline, making it much less likely to get reviewed."
        )
    if padded:
        submitter_flags.append(
            f"Description is heavily structured ({padding_headers} section headers, {padding_words} words) for "
            f"how small the change is ({diff_lines} lines). This can read as padded/templated to reviewers - but "
            f"it's also the same shape as a genuinely thorough writeup (e.g. a detailed security analysis), so "
            f"take it as a prompt to double-check, not a verdict."
        )
    if idle_days > STALE_DAYS and review_decision == "CHANGES_REQUESTED":
        submitter_flags.append(
            f"A reviewer requested changes {int(idle_days)} days ago with no follow-up activity since."
        )
    elif idle_days > STALE_DAYS and review_decision == "NONE":
        submitter_flags.append(f"No review activity at all in {int(idle_days)} days.")

    score = 0.0
    if is_draft:
        score -= 100  # sinks to the bottom, still visible for triage
    score -= min(age_days / 30 * 20, 25)  # older PRs lose context/relevance and are more likely stale or superseded
    if tested:
        score += 8
    if author_is_contributor:
        score += 8  # established contributor, not a first-time/anonymous drive-by
    if has_contributor_comment:
        score += 25  # another established contributor already engaged/vetted this
    if links_issue:
        score += 20  # tied to a real, tracked problem rather than a speculative change
    score += min(non_author_comments * 6 + reaction_count * 3, 35)  # outside interest, diminishing returns
    if last_action_by_author:
        score += 6  # ball's in the reviewer's court, not waiting on the submitter
    # NOTE: paddedDescription is intentionally NOT scored - it can't be told
    # apart from a genuinely thorough writeup by structure alone (see
    # is_padded_description's docstring). Surfaced as a flag only.
    if ci == "PASSING":
        score += 20
    elif ci == "FAILING":
        score -= 30
    # PENDING/NONE: no adjustment either way - CI hasn't rendered a verdict yet,
    # so there's nothing to reward or punish (see also the CI-approval gate
    # note below - "hasn't run" is frequently not the submitter's doing).
    if mergeable == "MERGEABLE":
        score += 15
    elif mergeable == "CONFLICTING":
        score -= 25
    score += diff_size_score(diff_lines)
    if desc_state == "OK":
        score += 10
    else:
        score -= 10
    if review_decision == "APPROVED":
        score += 25  # just needs a merge
    elif review_decision == "CHANGES_REQUESTED" and idle_days > STALE_DAYS:
        score -= 15  # ball's in the author's court, not a reviewer's
    elif review_decision == "NONE":
        score += 10  # hasn't had a first look yet
    if suspicious_marker:
        score -= 60  # likely gaming CONTRIBUTING.md's agent-fast-track note, not a merit signal

    reviewer_reasons = []
    if review_decision == "APPROVED":
        reviewer_reasons.append("Already approved — just needs a maintainer to merge.")
    if ci == "PASSING" and mergeable == "MERGEABLE" and diff_lines < 100 and desc_state == "OK":
        reviewer_reasons.append("Small, clean, CI green, well-described — low-friction review.")
    if review_decision == "NONE" and ci not in ("FAILING", "PENDING") and mergeable != "CONFLICTING" and not is_draft:
        reviewer_reasons.append(
            "No formal review (approve/request changes) yet — though it may have discussion/comments."
        )
    if tested:
        reviewer_reasons.append("Description mentions testing performed — more confidence it actually works.")
    if author_is_contributor:
        reviewer_reasons.append("Author is an established project contributor.")
    if has_contributor_comment:
        reviewer_reasons.append("Another contributor has already engaged in discussion on this PR.")
    if links_issue:
        reviewer_reasons.append("Linked to a tracked issue rather than a speculative change.")
    if non_author_activity > 0:
        reviewer_reasons.append(
            f"Drawn outside interest: {non_author_comments} comment(s), {reaction_count} reaction(s) from others."
        )
    if last_action_by_author:
        reviewer_reasons.append("Author replied last — waiting on a reviewer, not stalled on the submitter.")

    return {
        "number": number,
        "title": title,
        "author": pr["author"]["login"],
        "url": pr["url"],
        "base": pr["baseRefName"],
        "createdAt": pr["createdAt"],
        "updatedAt": pr["updatedAt"],
        "ageDays": round(age_days, 1),
        "idleDays": round(idle_days, 1),
        "diffLines": diff_lines,
        "filesChanged": files,
        "isDraft": is_draft,
        "ci": ci,
        "mergeable": mergeable,
        "reviewDecision": review_decision,
        "hasReviewerRequested": has_reviewer,
        "descriptionState": desc_state,
        "mentionsTesting": tested,
        "authorIsContributor": author_is_contributor,
        "hasContributorComment": has_contributor_comment,
        "linksIssue": links_issue,
        "nonAuthorComments": non_author_comments,
        "reactionCount": reaction_count,
        "lastActionByAuthor": last_action_by_author,
        "paddedDescription": padded,
        "descriptionHeaderCount": padding_headers,
        "suspiciousAgentMarker": suspicious_marker,
        "reviewerScore": round(score, 1),
        "submitterFeedback": submitter_flags,
        "reviewerReasons": reviewer_reasons,
    }


def build_markdown(analyzed, repo):
    reviewable = [p for p in analyzed if not p["isDraft"]]
    reviewable.sort(key=lambda p: p["reviewerScore"], reverse=True)
    abandoned = [
        p for p in analyzed
        if p["idleDays"] > STALE_DAYS and p["reviewDecision"] == "CHANGES_REQUESTED"
    ]
    abandoned.sort(key=lambda p: p["idleDays"], reverse=True)

    lines = [f"# PR Triage Report — {repo}", "",
             f"Generated {datetime.now(timezone.utc).isoformat()}", "",
             f"Open PRs analyzed: {len(analyzed)}", "",
             "## Top candidates to review now", "",
             "| # | Title | Author | Score | CI | Merge | Diff | Age (d) |",
             "|---|---|---|---|---|---|---|---|"]
    for p in reviewable[:20]:
        title = p["title"].replace("|", "\\|")[:60]
        lines.append(
            f"| [{p['number']}]({p['url']}) | {title} | {p['author']} | "
            f"{p['reviewerScore']} | {p['ci']} | {p['mergeable']} | {p['diffLines']} | {p['ageDays']} |"
        )
    lines += ["", "## Likely abandoned (changes requested, no follow-up)", ""]
    if abandoned:
        lines += ["| # | Title | Author | Idle (d) |", "|---|---|---|---|"]
        for p in abandoned[:20]:
            title = p["title"].replace("|", "\\|")[:60]
            lines.append(f"| [{p['number']}]({p['url']}) | {title} | {p['author']} | {p['idleDays']} |")
    else:
        lines.append("None found.")
    lines.append("")
    return "\n".join(lines)


def build_dashboard(analyzed, repo):
    template_path = Path(__file__).parent / "dashboard_template.html"
    template = template_path.read_text()
    data_json = json.dumps(analyzed)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        template
        .replace("__DATA__", data_json)
        .replace("__REPO__", repo)
        .replace("__GENERATED__", generated)
        .replace("__COUNT__", str(len(analyzed)))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--skip-extras", action="store_true",
                         help="Skip per-PR lookups (merge conflicts, author association, linked issues) - faster, less data")
    parser.add_argument("--out-dir", default=str(Path(__file__).parent / "out"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching open PRs from {args.repo}...", file=sys.stderr)
    prs = fetch_prs(args.repo, args.limit)
    print(f"Fetched {len(prs)} PRs.", file=sys.stderr)

    extras = {}
    if not args.skip_extras:
        print(f"Fetching per-PR extras (merge status, author association, linked issues) for {len(prs)} PRs...",
              file=sys.stderr)
        extras = fetch_pr_extras(args.repo, [p["number"] for p in prs])

    now = datetime.now(timezone.utc)
    analyzed = [analyze(pr, extras, now) for pr in prs]

    (out_dir / "prs.json").write_text(json.dumps(analyzed, indent=2))
    (out_dir / "report.md").write_text(build_markdown(analyzed, args.repo))
    (out_dir / "dashboard.html").write_text(build_dashboard(analyzed, args.repo))

    print(f"Wrote {out_dir / 'prs.json'}", file=sys.stderr)
    print(f"Wrote {out_dir / 'report.md'}", file=sys.stderr)
    print(f"Wrote {out_dir / 'dashboard.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
