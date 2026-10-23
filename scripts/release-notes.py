#!/usr/bin/env python3
"""Next release tag and bullet-point notes for main after a green CI run (job `release` in ci.yml).

  scripts/release-notes.py --target <sha> --run-url <url> --out notes.md  # the tag, or nothing
  scripts/release-notes.py --target origin/main                           # preview to stdout
  scripts/release-notes.py --self-test

Work reaches develop through PRs; a release PR merges develop into main. Reads the commits
between the newest vX.Y.Z tag and the target and keeps one bullet per PR: "Merge pull request #N"
carries the PR title as its body, a squash merge ends its subject with "(#N)". A merge from
.../develop is the release PR and is named in the header, not listed. Commits inside a PR, rebase
merges and direct pushes are not listed. A title follows `type(scope): subject`. The bump: `!`
after the type or BREAKING CHANGE -> major, any feat -> minor, else patch; with no tag yet the
first release is FIRST. Prints nothing when the range is empty, so a re-run, or a late run for a
commit a newer release already holds, makes no release.
Exit code: 0 done, 1 self-test failed, 2 git error.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
SQUASH = re.compile(r"^(.+) \(#(\d+)\)$")
TITLE = re.compile(r"^(\w+)(?:\(([^)]*)\))?(!)?:\s*(.+)$")
GROUPS = [("feat", "Features"), ("fix", "Fixes"), ("docs", "Docs"), (None, "Other")]
FIRST = (1, 0, 0)
RELEASE_BRANCH = "/develop"
HEADER = "Released by CI ({run_url}) on {today}, after green tests on main"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def parse_tag(name):
    m = TAG.match(name)
    return tuple(int(x) for x in m.groups()) if m else None


def classify(commits):
    """[(sha, subject, body)] -> (release PR numbers, [(type, bullet, breaking)])."""
    releases, items = [], []
    for _, subject, body in commits:
        if m := MERGE.match(subject):
            if m.group(2).endswith(RELEASE_BRANCH):
                releases.append(m.group(1))
                continue
            title = next((line.strip() for line in body.splitlines() if line.strip()), subject)
            number = m.group(1)
        elif m := SQUASH.match(subject):
            title, number = m.groups()
        else:
            continue
        if m := TITLE.match(title):
            kind, scope, bang, text = m.groups()
            text = f"{scope}: {text}" if scope else text
        else:
            kind, bang, text = None, None, title
        items.append((kind, f"{text} (#{number})", bool(bang) or "BREAKING CHANGE" in body))
    return releases, items


def bump(prev, items):
    major, minor, patch = prev
    if any(b for _, _, b in items):
        return (major + 1, 0, 0)
    if any(k == "feat" for k, _, _ in items):
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def render(prev_tag, new_tag, releases, items, run_url, today, repo):
    if run_url:
        head = HEADER.format(run_url=run_url, today=today)
        head += f", from release PR #{releases[-1]}." if releases else "."
    else:
        head = f"Preview on {today}, not released."
    lines = [head, ""]
    known = {k for k, _ in GROUPS if k}
    for kind, heading in GROUPS:
        bullets = [t for k, t, _ in items if (k == kind if kind else k not in known)]
        if bullets:
            lines += [f"### {heading}", *(f"- {b}" for b in bullets), ""]
    if repo:
        span = f"compare/{prev_tag}...{new_tag}" if prev_tag else f"commits/{new_tag}"
        lines.append(f"**Full Changelog**: https://github.com/{repo}/{span}")
    return "\n".join(lines).rstrip() + "\n"


def commits_between(prev_tag, target):
    span = f"{prev_tag}..{target}" if prev_tag else target
    # Not --first-parent: the PRs merged into develop sit behind the release PR's merge.
    return parse_log(git("log", "--format=%H%x1f%s%x1f%b%x1e", span))


def parse_log(log):
    # strip("\n"), not strip(): str.strip() also eats \x1f and would drop an empty body's field.
    records = filter(None, (r.strip("\n") for r in log.split("\x1e")))
    return [tuple(r.split("\x1f", 2)) for r in records]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="HEAD")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--out", help="write the notes here and print only the tag")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    try:
        tags = sorted((parse_tag(t), t) for t in git("tag", "--list", "v*").split() if parse_tag(t))
        prev, prev_tag = tags[-1] if tags else (None, "")
        commits = commits_between(prev_tag, args.target)
    except subprocess.CalledProcessError as e:
        print(f"release-notes: git {e.cmd[1]} failed: {e.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    if not commits:
        return 0
    releases, items = classify(commits)
    new_tag = "v{}.{}.{}".format(*(bump(prev, items) if prev else FIRST))
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    notes = render(prev_tag, new_tag, releases, items, args.run_url, today, repo)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(notes)
        print(new_tag)
    else:
        print(new_tag)
        print(notes, end="")
    return 0


def self_test():
    commits = [
        ("1" * 40, "Merge pull request #9 from o/develop", "release: develop to main"),
        ("a" * 40, "Merge pull request #7 from o/fix/m", "fix(frontend): keep the modal in step\n"),
        ("b" * 40, "feat(auth): sign up from an invite link (#6)", "* feat(auth): ...\n* fix: ..."),
        ("c" * 40, "docs: Proton Mail SMTP example (#5)", ""),
        ("9" * 40, "fix: a commit inside PR #7, or a direct push", ""),
        ("d" * 40, "Merge pull request #2 from o/claude/thread", "EasyToDo v2.0"),
        ("e" * 40, "Merge pull request #1 from o/x", ""),
    ]
    releases, items = classify(commits)
    assert releases == ["9"], releases
    assert items[0] == ("fix", "frontend: keep the modal in step (#7)", False), items[0]
    assert items[1] == ("feat", "auth: sign up from an invite link (#6)", False), items[1]
    assert items[2] == ("docs", "Proton Mail SMTP example (#5)", False), items[2]
    assert items[3] == (None, "EasyToDo v2.0 (#2)", False), items[3]
    assert items[4][1] == "Merge pull request #1 from o/x (#1)" and len(items) == 5, items
    assert bump((2, 0, 0), items) == (2, 1, 0)
    assert bump((2, 1, 3), [i for i in items if i[0] != "feat"]) == (2, 1, 4)
    _, breaking = classify(
        [
            ("f" * 40, "feat(x)!: drop y (#9)", ""),
            ("0" * 40, "Merge pull request #8 from o/b", "fix: z\n\nBREAKING CHANGE: w"),
        ]
    )
    assert [b for _, _, b in breaking] == [True, True] and bump((2, 1, 0), breaking) == (3, 0, 0)

    text = render("v2.0.0", "v2.1.0", releases, items, "https://run/1", "2026-10-24", "o/r")
    head = "Released by CI (https://run/1) on 2026-10-24, after green tests on main, from release PR #9.\n"
    assert text.startswith(head), text
    assert "### Features\n- auth: sign up from an invite link (#6)\n" in text, text
    assert "### Other\n- EasyToDo v2.0 (#2)\n" in text, text
    order = [text.index(f"### {h}") for _, h in GROUPS]
    assert order == sorted(order), order
    assert text.rstrip().endswith("compare/v2.0.0...v2.1.0"), text
    first = render("", "v1.0.0", [], [], "", "d", "o/r")
    assert first.startswith("Preview on d") and "###" not in first, first
    assert first.rstrip().endswith("commits/v1.0.0"), first
    assert parse_tag("v2.10.0") == (2, 10, 0) and parse_tag("legacy-v1") is None
    log = "a\x1fdocs: x\x1f\x1e\nb\x1fMerge pull request #1 from o/x\x1fT\n\x1e\n"
    assert parse_log(log) == [("a", "docs: x", ""), ("b", "Merge pull request #1 from o/x", "T")]
    print("release-notes self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
