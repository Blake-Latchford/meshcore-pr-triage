#!/usr/bin/env python3
"""PR and issue triage tool for meshcore-dev/MeshCore.

Fetches all open PRs and issues, flags likely reasons each one is stalled
(for feedback to the submitter/reporter), and computes a priority score (for
maintainers deciding what to look at next). Requires the `gh` CLI to be
authenticated.

Outputs, under --out-dir (default ./out):
  prs.json        - full computed data per PR
  issues.json     - full computed data per issue
  report.md       - terminal/markdown summary (top review candidates,
                    likely-abandoned PRs, issues needing attention, issues
                    about to be auto-closed, issues with a fix pending)
  dashboard.html  - static, self-contained, sortable/filterable tables
                    (PRs and issues tabs); open directly in a browser, no
                    server needed
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

ISSUE_FIELDS = ",".join([
    "number", "title", "author", "createdAt", "updatedAt", "labels", "url",
    "body", "comments", "reactionGroups", "assignees",
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


# The weights below were fit against every PR to dev from 2025-03 to
# 2026-09 (merged vs. closed-or-still-open). On that history, the author's
# merge track record, diff size, and description length are what separate
# merged PRs from the rest; mentioning testing, linking an issue, and having
# a non-empty description made no difference once those were accounted for,
# so they're surfaced as information but not scored. Scoring this way ranks
# historical outcomes at AUC 0.81, vs. 0.64 for the earlier weights.

# Author track record: log-scaled, so the first few merges matter most.
# Merge rate for non-maintainers went 32% (no prior merges) -> 45% (1-2) ->
# 58% (3-9) -> 75% (10+).
PRIOR_MERGE_LOG_WEIGHT = 12
PRIOR_MERGE_BONUS_MAX = 50


def prior_merge_score(merged_count):
    if not merged_count:
        return 0
    return min(PRIOR_MERGE_BONUS_MAX, PRIOR_MERGE_LOG_WEIGHT * math.log2(merged_count + 1))


# Description length (scored on describe_body's cleaned character count):
# longer descriptions merged steadily less often - by raw body length, 66%
# under 200 characters, 27% at 1000-3000, 15% past 3000 - and that held within every experience level,
# so it's not just first-timers writing more. Unlike is_padded_description
# this doesn't look at structure or compare against diff size; it's a plain
# length penalty, so a long genuine writeup is demoted too, as it was in
# practice.
LONG_DESC_CHARS = 300
LONG_DESC_LOG_WEIGHT = 12
LONG_DESC_PENALTY_MAX = 30
LONG_DESC_FLAG_CHARS = 1500


def description_length_score(desc_len):
    if desc_len <= LONG_DESC_CHARS:
        return 0
    return -min(LONG_DESC_PENALTY_MAX, LONG_DESC_LOG_WEIGHT * math.log2(desc_len / LONG_DESC_CHARS))


GH_RETRY_ATTEMPTS = 4
GH_RETRY_BACKOFF = 1.5  # seconds; doubles each attempt (1.5, 3, 6)

# Fetching the full BULK_FIELDS set (statusCheckRollup in particular) for
# every open PR in one `gh pr list` call reliably 502s / truncates once a
# repo has a few hundred open PRs, because GitHub computes checks lazily per
# PR on its backend rather than in bulk. Splitting the fetch into windows of
# this size keeps each call fast (single digit seconds) and well clear of
# that limit.
BULK_CHUNK_SIZE = 50


def run_gh(args):
    last_err = None
    for attempt in range(GH_RETRY_ATTEMPTS):
        if attempt:
            time.sleep(GH_RETRY_BACKOFF * (2 ** (attempt - 1)))
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        last_err = result.stderr.strip()
    raise RuntimeError(f"gh {' '.join(args)} failed after {GH_RETRY_ATTEMPTS} attempts: {last_err}")


def fetch_open(kind, repo, limit, fields):
    """kind is "pr" or "issue" - `gh pr list` and `gh issue list` take the
    same flags, so both go through the same chunked fetch."""
    # Cheap first pass: just enough per item to build date-bounded chunks for
    # the expensive fetch below. This call is light enough to survive at any
    # item count even though the full-field one isn't.
    out = run_gh([
        kind, "list", "--repo", repo, "--state", "open", "--limit", str(limit),
        "--json", "number,createdAt",
    ])
    dated = json.loads(out)
    if not dated:
        return []
    dated.sort(key=lambda p: p["createdAt"])

    all_items = {}
    chunks = [dated[i:i + BULK_CHUNK_SIZE] for i in range(0, len(dated), BULK_CHUNK_SIZE)]
    for i, chunk in enumerate(chunks):
        start, end = chunk[0]["createdAt"], chunk[-1]["createdAt"]
        print(f"  fetching {kind}s {i + 1}/{len(chunks)} ({start}..{end})...", file=sys.stderr)
        # GitHub's search backend occasionally returns an empty or partial
        # result set with a success status, which would silently drop a whole
        # window. The first pass says exactly which numbers belong here, so
        # check against that and retry until they're all present.
        expected = {p["number"] for p in chunk}
        for attempt in range(GH_RETRY_ATTEMPTS):
            if attempt:
                time.sleep(GH_RETRY_BACKOFF * (2 ** (attempt - 1)))
            out = run_gh([
                kind, "list", "--repo", repo, "--state", "open",
                # created-date window instead of a flat --limit/offset: gh has no
                # offset, and narrowing by search is what keeps each call's
                # server-side work (and 502 risk) small.
                "--search", f"created:{start}..{end}",
                "--limit", str(len(chunk) + 10),  # slack for same-instant items at the window edge
                "--json", fields,
            ])
            for item in json.loads(out):
                all_items[item["number"]] = item  # dedupe items shared by adjacent windows' inclusive bounds
            missing = expected - all_items.keys()
            if not missing:
                break
            print(f"    {len(missing)} {kind}(s) missing from search results, retrying...", file=sys.stderr)
        else:
            # Can legitimately happen if an item was closed between the two
            # passes, so warn rather than fail.
            print(f"  WARNING: {len(missing)} {kind}(s) never came back from search: "
                  f"{', '.join(f'#{n}' for n in sorted(missing))}", file=sys.stderr)
    return list(all_items.values())


def fetch_prs(repo, limit):
    return fetch_open("pr", repo, limit, BULK_FIELDS)


def fetch_issues(repo, limit):
    return fetch_open("issue", repo, limit, ISSUE_FIELDS)


def fetch_latest_firmware_minor(repo):
    """Minor version of the newest firmware release (tags look like
    companion-v1.18.0), so issues reported against much older firmware can
    be asked to retest. None if it can't be determined."""
    try:
        out = run_gh(["release", "list", "--repo", repo, "--limit", "30", "--json", "tagName"])
    except RuntimeError:
        return None
    minors = [
        int(m.group(1))
        for r in json.loads(out)
        if (m := re.search(r"v1\.(\d+)", r["tagName"]))
    ]
    return max(minors) if minors else None


def get_github_token():
    return run_gh(["auth", "token"]).strip()


def graphql_session():
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {get_github_token()}"})
    # Transport-level failures (5xx, rate limiting, connection resets) get
    # retried with exponential backoff by urllib3 itself: 0.5, 1, 2, 4s.
    retry = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


# Logins per GraphQL request when counting merged PRs: each author is one
# aliased search() field, so a batch is one round trip instead of N.
AUTHOR_BATCH_SIZE = 20


def fetch_author_merge_counts(repo, logins):
    """Number of merged PRs each author already has in the repo (any base
    branch). This is the strongest single predictor of a PR being merged in
    the repo's history - far stronger than GitHub's authorAssociation, which
    only says whether there's at least one. None for an author whose lookup
    failed, so "unknown" isn't mistaken for "first-timer"."""
    session = graphql_session()
    logins = sorted(logins)
    counts = {}

    def one(batch):
        fields = "\n".join(
            # json.dumps produces a valid GraphQL string literal.
            f"a{i}: search(query: {json.dumps(f'repo:{repo} is:pr is:merged author:{login}')}, "
            f"type: ISSUE, first: 0) {{ issueCount }}"
            for i, login in enumerate(batch)
        )
        for attempt in range(4):
            try:
                resp = session.post(GRAPHQL_URL, json={"query": f"query {{\n{fields}\n}}"}, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("errors"):
                    # Same HTTP-200 secondary rate limit as fetch_pr_extras.
                    if any(e.get("type") == "RATE_LIMIT" for e in payload["errors"]) and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(str(payload["errors"]))
                data = payload["data"]
                return {login: data[f"a{i}"]["issueCount"] for i, login in enumerate(batch)}
            except (requests.RequestException, RuntimeError, KeyError, TypeError):
                if attempt == 3:
                    break
        return {login: None for login in batch}

    batches = [logins[i:i + AUTHOR_BATCH_SIZE] for i in range(0, len(logins), AUTHOR_BATCH_SIZE)]
    # Few workers: search has a tighter secondary rate limit than plain queries.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(one, batches):
            counts.update(result)
    return counts


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
    session = graphql_session()
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


def analyze(pr, extras, merge_counts, now):
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
    author_merged_prs = merge_counts.get(author_login)  # None = unknown
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
    if desc_len > LONG_DESC_FLAG_CHARS:
        submitter_flags.append(
            f"Long description (~{desc_len} characters). Historically, PRs here with long descriptions were "
            f"merged far less often than ones with a few sentences on what changed and why."
        )
    if author_merged_prs == 0:
        submitter_flags.append(
            "First PR to this repo. Historically about a third of first PRs get merged, vs. three quarters for "
            "authors with 10+ merged; a small, focused fix is the most reliable way in."
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
    # Not scored (see the weights note above prior_merge_score): tested,
    # links_issue, and an empty/short description.
    score += prior_merge_score(author_merged_prs)
    if has_contributor_comment:
        score += 25  # another established contributor already engaged/vetted this
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
    score += description_length_score(desc_len)
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
    if author_merged_prs:
        reviewer_reasons.append(f"Author has {author_merged_prs} merged PR(s) in this repo already.")
    elif author_is_contributor:
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
        "authorMergedPRs": author_merged_prs,
        "descriptionLength": desc_len,
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


ISSUE_REFERENCE_RE = re.compile(r"(?<![\w/])#(\d+)\b")


def open_prs_by_issue(prs, extras, open_issue_numbers):
    """Map issue number -> open PR numbers that look like they address it.

    Built entirely from data the PR pass already fetched: closing keywords
    ("Fixes #N"), issues that cross-referenced the PR, and bare #N mentions
    in the PR body. PRs and issues share one number space, so filtering to
    open issue numbers drops mentions of other PRs."""
    links = {}
    for pr in prs:
        extra = extras.get(pr["number"], {})
        refs = {n["number"] for n in (extra.get("closingIssuesReferences") or {}).get("nodes") or []}
        refs |= {
            item["source"]["number"]
            for item in (extra.get("timelineItems") or {}).get("nodes") or []
            if (item.get("source") or {}).get("__typename") == "Issue"
        }
        refs |= {int(n) for n in ISSUE_REFERENCE_RE.findall(pr.get("body") or "")}
        for n in refs & open_issue_numbers:
            links.setdefault(n, []).append(pr["number"])
    return {n: sorted(v) for n, v in links.items()}


# Maintainer associations specifically - narrower than TRUSTED_ASSOCIATIONS,
# since a CONTRIBUTOR replying on an issue isn't the project acknowledging it.
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# The repo's stale bot (.github/workflows/stale-bot.yml) labels an issue
# `stale` after 60 days without activity and closes it 7 days later unless
# it's labelled `keep-open` or someone comments.
STALE_BOT_CLOSE_DAYS = 7
STALE_LABEL = "stale"
KEEP_OPEN_LABEL = "keep-open"
STALE_BOT_MESSAGE = "This issue is stale"

ISSUE_MIN_BODY_CHARS = 80
NO_RESPONSE_GRACE_DAYS = 3  # don't flag "no response" on issues filed this week
FW_BEHIND_MINORS = 2  # reports this many minor releases behind get a "please retest" nudge
RESCUE_MIN_INTEREST = 3  # stale issues with at least this much interest are worth a keep-open look


def is_bot(login):
    return login == "github-actions" or login.endswith("[bot]") or login.endswith("-bot")


def issue_kind(issue):
    labels = {l["name"] for l in issue.get("labels") or []}
    title = issue["title"]
    if "New Hardware Support" in labels or re.search(
            r"device support|hardware support|new board|board variant|add support for .*(board|device)", title, re.I):
        return "HARDWARE"
    if "bug" in labels:
        return "BUG"
    if "enhancement" in labels or re.search(
            r"feature|enhancement|proposal|suggestion|\brfc\b|wish|request|\badd\b|support for|allow|option to|\bshould\b", title, re.I):
        return "FEATURE"
    if "question" in labels or title.rstrip().endswith("?") or re.match(r"\s*(how|can|is|does|why)\b", title, re.I):
        return "QUESTION"
    if re.search(r"\bbug\b|crash|not working|stops? working|(?:doesn'?t|does not|won'?t|not) work|broken|"
                 r"\bfail|error|lock-?up|freez|reboot|\bhang|not (?:receiving|sending|showing|connecting)|"
                 r"regression|wrong|incorrect|corrupt|\blost\b|drain|consum|\bno (?:serial|ble|gps|display)|"
                 r"can'?t|cannot|unable|always|never|stuck|missing|issue|problem",
                 title, re.I):
        return "BUG"
    return "OTHER"


# Heuristics for the pieces CONTRIBUTING.md asks bug reports to include. Loose
# on purpose - a false "missing" costs the reporter one extra sentence, but
# these exist to explain why a report might be hard to act on, not to grade it.
REPRO_RE = re.compile(r"reproduc|repro\b|steps|to replicate|how to trigger|(?m:^\s*1[.)]\s.*\n\s*2[.)]\s)", re.I)
HARDWARE_RE = re.compile(
    r"heltec|\brak\b|rak\d{3,}|t-?deck|t-?beam|t-?echo|t-?lora|lilygo|\bwio\b|xiao|seeed|station ?g[12]|"
    r"thinknode|ikoka|nrf52|esp32|sx12\d\d|\bboard\b|\bv[234]\b|tracker|sensecap|promicro|pro micro",
    re.I,
)
FW_VERSION_RE = re.compile(
    r"(?:firmware|\bfw\b|companion|repeater|room ?server|version)[^\n]{0,30}?\bv?1\.(\d{1,2})(?:\.\d+)?\b"
    r"|\bv1\.(\d{1,2})(?:\.\d+)?\b",
    re.I,
)
ME_TOO_RE = re.compile(
    r"\b(me too|same here|same (?:issue|problem|thing)|also (?:seeing|having|experiencing|affected)|"
    r"can confirm|confirmed|i see this too|happening to me|\+1)\b|^\s*\+1",
    re.I,
)


def firmware_minor_mentioned(body, latest_minor):
    """Highest firmware minor version the report mentions, ignoring numbers
    above the latest release (those are almost always app versions like
    1.50, which share the 1.x shape)."""
    minors = [int(a or b) for a, b in FW_VERSION_RE.findall(body or "")]
    if latest_minor is not None:
        minors = [m for m in minors if m <= latest_minor]
    return max(minors) if minors else None


def analyze_issue(issue, linked_prs, latest_minor, now):
    number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""
    author_login = issue["author"]["login"]
    labels = [l["name"] for l in issue.get("labels") or []]
    kind = issue_kind(issue)
    age_days = days_since(issue["createdAt"], now)
    assignees = [a["login"] for a in issue.get("assignees") or []]

    comments = issue.get("comments") or []
    human_comments = [c for c in comments if c.get("author") and not is_bot(c["author"]["login"])]
    # updatedAt gets bumped by the stale bot's own comment and by label edits,
    # so idle time is measured from the last human comment instead - otherwise
    # every bot-pinged issue looks freshly active.
    last_human_at = human_comments[-1]["createdAt"] if human_comments else issue["createdAt"]
    idle_days = days_since(last_human_at, now)
    others = [c for c in human_comments if c["author"]["login"] != author_login]
    responders = {c["author"]["login"] for c in others}
    maintainer_responded = any(c.get("authorAssociation") in MAINTAINER_ASSOCIATIONS for c in others)
    last_action_by_author = bool(human_comments) and human_comments[-1]["author"]["login"] == author_login
    last_by_maintainer = bool(human_comments) and not last_action_by_author \
        and human_comments[-1].get("authorAssociation") in MAINTAINER_ASSOCIATIONS
    confirmations = {c["author"]["login"] for c in others if ME_TOO_RE.search(c.get("body") or "")}
    # gh's bulk reactionGroups only carries per-emoji totals, not who reacted,
    # so unlike the PR side this can't dedupe one person using several emoji.
    reactions = sum((g.get("users") or {}).get("totalCount", 0) for g in issue.get("reactionGroups") or [])
    interest = len(responders) + reactions

    is_stale = STALE_LABEL in labels and KEEP_OPEN_LABEL not in labels
    closes_in_days = None
    if is_stale:
        marked = [c["createdAt"] for c in comments
                  if c.get("author") and is_bot(c["author"]["login"]) and STALE_BOT_MESSAGE in (c.get("body") or "")]
        since_marked = days_since(marked[-1], now) if marked else 0
        closes_in_days = max(0, round(STALE_BOT_CLOSE_DAYS - since_marked, 1))

    body_len = len(re.sub(r"\s", "", body))
    short_body = body_len < ISSUE_MIN_BODY_CHARS
    has_repro = bool(REPRO_RE.search(body))
    has_hardware = bool(HARDWARE_RE.search(body) or HARDWARE_RE.search(title))
    fw_minor = firmware_minor_mentioned(f"{title}\n{body}", latest_minor)
    has_version = fw_minor is not None
    fw_outdated = (kind == "BUG" and has_version and latest_minor is not None
                   and latest_minor - fw_minor >= FW_BEHIND_MINORS)
    missing = []
    if kind == "BUG":
        if not has_repro:
            missing.append("steps to reproduce")
        if not has_hardware:
            missing.append("board/hardware")
        if not has_version:
            missing.append("firmware version")

    reporter_flags = []
    if closes_in_days is not None:
        # actions/stale only processes a limited number of issues per run, so
        # past-due issues can sit open for a while before it gets to them.
        when = (f"in about {closes_in_days:g} day(s)" if closes_in_days
                else "on one of the bot's next runs (it's past its 7-day grace period)")
        reporter_flags.append(
            f"Marked stale by the bot — will auto-close {when} unless someone "
            f"comments or a maintainer adds `{KEEP_OPEN_LABEL}`."
        )
    if short_body:
        reporter_flags.append("Issue description is very short, giving maintainers little to go on.")
    if missing:
        reporter_flags.append(
            f"Bug report doesn't appear to include: {', '.join(missing)} — CONTRIBUTING.md asks for these, "
            f"and without them a maintainer can't easily reproduce the problem."
        )
    if fw_outdated:
        reporter_flags.append(
            f"Reported against firmware 1.{fw_minor}; the latest release is 1.{latest_minor}. "
            f"Retesting on current firmware and confirming it still happens makes it much more actionable."
        )
    if not responders and age_days > NO_RESPONSE_GRACE_DAYS:
        reporter_flags.append(f"Nobody besides the reporter has responded in {int(age_days)} days.")
    if last_by_maintainer and idle_days > STALE_DAYS:
        reporter_flags.append(
            f"A maintainer replied {int(idle_days)} days ago with no response from anyone since — "
            f"likely waiting on more information from the reporter."
        )

    score = 0.0
    score -= min(idle_days / 30 * 10, 20)  # long-quiet issues are more likely fixed, moot, or abandoned
    score += min(len(responders) * 5 + reactions * 3, 40)  # breadth of interest, diminishing returns
    if kind == "BUG":
        score += 10  # broken behaviour generally outranks new asks
        if confirmations:
            score += 15  # independently reproduced by someone else
        if not missing:
            score += 10  # actionable as written
        score -= 5 * len(missing)
        if fw_outdated:
            score -= 10  # may already be fixed
    if linked_prs:
        score += 20  # reviewing an existing PR may close this outright
    if not responders and age_days > NO_RESPONSE_GRACE_DAYS:
        score += 10  # hasn't had a first look yet
    if last_action_by_author and responders:
        score += 6  # reporter answered follow-ups, ball's in the maintainers' court
    if maintainer_responded:
        score += 5  # already acknowledged/triaged, likely real
    if short_body:
        score -= 10

    maintainer_reasons = []
    if linked_prs:
        refs = ", ".join(f"#{n}" for n in linked_prs)
        maintainer_reasons.append(f"Open PR(s) {refs} may address this — reviewing them could close it.")
    if closes_in_days is not None and interest >= RESCUE_MIN_INTEREST:
        maintainer_reasons.append(
            f"About to be auto-closed despite outside interest ({interest}) — consider `{KEEP_OPEN_LABEL}` "
            f"or closing it deliberately."
        )
    if confirmations:
        maintainer_reasons.append(f"Confirmed by {len(confirmations)} other user(s) in the comments.")
    if kind == "BUG" and not missing:
        maintainer_reasons.append("Bug report includes repro steps, hardware, and firmware version.")
    if not responders and age_days > NO_RESPONSE_GRACE_DAYS:
        maintainer_reasons.append("Never had a response — a quick first look (or label) would help.")
    if interest > 0:
        maintainer_reasons.append(
            f"Drawn outside interest: {len(responders)} other participant(s), {reactions} reaction(s)."
        )
    if last_action_by_author and responders:
        maintainer_reasons.append("Reporter replied last — waiting on a maintainer, not on the reporter.")
    if assignees:
        maintainer_reasons.append(f"Assigned to {', '.join(assignees)}.")

    return {
        "number": number,
        "title": title,
        "author": author_login,
        "url": issue["url"],
        "kind": kind,
        "labels": labels,
        "assignees": assignees,
        "createdAt": issue["createdAt"],
        "lastHumanActivityAt": last_human_at,
        "ageDays": round(age_days, 1),
        "idleDays": round(idle_days, 1),
        "participants": len(responders),
        "reactionCount": reactions,
        "confirmations": len(confirmations),
        "maintainerResponded": maintainer_responded,
        "lastActionByAuthor": last_action_by_author,
        "linkedPRs": linked_prs,
        "isStale": is_stale,
        "closesInDays": closes_in_days,
        "missingDetails": missing,
        "firmwareMinor": fw_minor,
        "firmwareOutdated": fw_outdated,
        "shortDescription": short_body,
        "priorityScore": round(score, 1),
        "reporterFeedback": reporter_flags,
        "maintainerReasons": maintainer_reasons,
    }


def md_title(item):
    return item["title"].replace("|", "\\|")[:60]


def build_markdown(analyzed, issues, repo):
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
        title = md_title(p)
        lines.append(
            f"| [{p['number']}]({p['url']}) | {title} | {p['author']} | "
            f"{p['reviewerScore']} | {p['ci']} | {p['mergeable']} | {p['diffLines']} | {p['ageDays']} |"
        )
    lines += ["", "## Likely abandoned (changes requested, no follow-up)", ""]
    if abandoned:
        lines += ["| # | Title | Author | Idle (d) |", "|---|---|---|---|"]
        for p in abandoned[:20]:
            title = md_title(p)
            lines.append(f"| [{p['number']}]({p['url']}) | {title} | {p['author']} | {p['idleDays']} |")
    else:
        lines.append("None found.")
    lines.append("")
    if issues is not None:
        lines += build_issue_markdown(issues)
    return "\n".join(lines)


def issue_table(rows, columns):
    """columns: (header, fn(issue) -> cell) pairs, after the leading #/Title columns."""
    if not rows:
        return ["None found.", ""]
    out = ["| # | Title | " + " | ".join(h for h, _ in columns) + " |",
           "|---|---|" + "---|" * len(columns)]
    for i in rows[:20]:
        cells = " | ".join(str(fn(i)) for _, fn in columns)
        out.append(f"| [{i['number']}]({i['url']}) | {md_title(i)} | {cells} |")
    return out + [""]


def build_issue_markdown(issues):
    top = sorted((i for i in issues if not i["isStale"]), key=lambda i: i["priorityScore"], reverse=True)
    rescue = sorted(
        (i for i in issues if i["isStale"] and i["participants"] + i["reactionCount"] >= RESCUE_MIN_INTEREST),
        key=lambda i: (i["closesInDays"], -(i["participants"] + i["reactionCount"])),
    )
    fix_pending = sorted((i for i in issues if i["linkedPRs"]), key=lambda i: i["priorityScore"], reverse=True)
    unanswered = sorted(
        (i for i in issues if not i["participants"] and i["ageDays"] > NO_RESPONSE_GRACE_DAYS),
        key=lambda i: i["ageDays"], reverse=True,
    )
    prs = lambda i: ", ".join(f"#{n}" for n in i["linkedPRs"])
    return [
        f"# Issue Triage — {len(issues)} open issues", "",
        "## Top issues for maintainer attention", "",
        *issue_table(top, [("Kind", lambda i: i["kind"]), ("Score", lambda i: i["priorityScore"]),
                           ("People", lambda i: i["participants"]), ("Reactions", lambda i: i["reactionCount"]),
                           ("Open PRs", prs), ("Idle (d)", lambda i: i["idleDays"])]),
        "## About to be auto-closed, but people care", "",
        f"Labelled `{STALE_LABEL}` with at least {RESCUE_MIN_INTEREST} participants/reactions. "
        f"Add `{KEEP_OPEN_LABEL}` or close deliberately before the bot does.", "",
        *issue_table(rescue, [("Closes in (d)", lambda i: i["closesInDays"] or "overdue"),
                              ("People", lambda i: i["participants"]), ("Reactions", lambda i: i["reactionCount"])]),
        "## Fix pending in an open PR", "",
        *issue_table(fix_pending, [("Open PRs", prs), ("Kind", lambda i: i["kind"]),
                                   ("Score", lambda i: i["priorityScore"])]),
        "## Never answered (oldest first)", "",
        *issue_table(unanswered, [("Kind", lambda i: i["kind"]), ("Author", lambda i: i["author"]),
                                  ("Age (d)", lambda i: i["ageDays"])]),
    ]


def build_dashboard(analyzed, issues, repo):
    template_path = Path(__file__).parent / "dashboard_template.html"
    template = template_path.read_text()
    # </ is escaped so a "</script>" inside an issue/PR title can't end the
    # inline <script> block early.
    data_json = json.dumps({"prs": analyzed, "issues": issues or []}).replace("</", "<\\/")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        template
        .replace("__DATA__", data_json)
        .replace("__REPO__", repo)
        .replace("__GENERATED__", generated)
        .replace("__COUNT__", str(len(analyzed)))
        .replace("__ISSUE_COUNT__", str(len(issues)) if issues is not None else "no")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--skip-extras", action="store_true",
                         help="Skip per-PR and per-author lookups (merge conflicts, author association and merge "
                              "history, linked issues) - faster, less data")
    parser.add_argument("--skip-issues", action="store_true", help="Only triage PRs, not issues")
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

    merge_counts = {}
    if not args.skip_extras:
        authors = {p["author"]["login"] for p in prs}
        print(f"Counting previously merged PRs for {len(authors)} authors...", file=sys.stderr)
        merge_counts = fetch_author_merge_counts(args.repo, authors)

    now = datetime.now(timezone.utc)
    analyzed = [analyze(pr, extras, merge_counts, now) for pr in prs]

    analyzed_issues = None
    if not args.skip_issues:
        print(f"Fetching open issues from {args.repo}...", file=sys.stderr)
        issues = fetch_issues(args.repo, args.limit)
        print(f"Fetched {len(issues)} issues.", file=sys.stderr)
        latest_minor = fetch_latest_firmware_minor(args.repo)
        links = open_prs_by_issue(prs, extras, {i["number"] for i in issues})
        analyzed_issues = [analyze_issue(i, links.get(i["number"], []), latest_minor, now) for i in issues]
        (out_dir / "issues.json").write_text(json.dumps(analyzed_issues, indent=2))

    (out_dir / "prs.json").write_text(json.dumps(analyzed, indent=2))
    (out_dir / "report.md").write_text(build_markdown(analyzed, analyzed_issues, args.repo))
    (out_dir / "dashboard.html").write_text(build_dashboard(analyzed, analyzed_issues, args.repo))

    print(f"Wrote {out_dir / 'prs.json'}", file=sys.stderr)
    if analyzed_issues is not None:
        print(f"Wrote {out_dir / 'issues.json'}", file=sys.stderr)
    print(f"Wrote {out_dir / 'report.md'}", file=sys.stderr)
    print(f"Wrote {out_dir / 'dashboard.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
