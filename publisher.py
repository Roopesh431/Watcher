"""Publish regenerated docs as a pull request.

Part 3 of the docs agent: take the merged README from docs_gen.py, push it to a
fresh docs/auto-update-<sha> branch through the GitHub API, and open a pull
request whose body lists what changed in code and what changed in docs.
Nothing is pushed when the docs did not meaningfully change.

Usage:
    python publisher.py owner/repo
    python publisher.py owner/repo --from-file README.generated.md
    python publisher.py owner/repo --dry-run
    python publisher.py owner/repo --no-pr

Configuration (.env next to this file, or real environment variables):
    GITHUB_TOKEN, GITHUB_REPO               same as watcher.py
    LLM_API_KEY, LLM_MODEL, LLM_BASE_URL   used when --from-file is not given
"""

from __future__ import annotations

import argparse
import difflib
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv
from github import Auth, Github, GithubException, RateLimitExceededException, UnknownObjectException
from github.Commit import Commit
from github.ContentFile import ContentFile
from github.Repository import Repository

import docs_gen
import watcher

ENV_FILE = watcher.ENV_FILE

MAX_BODY_CHARS = 60_000
MAX_DIFF_LINES = 300

paint = watcher.paint
stamp = watcher.stamp
warn = watcher.warn
error = watcher.error
describe = watcher.describe


class PublishError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# meaningful diff
# --------------------------------------------------------------------------


def content_lines(text: str) -> list[str]:
    """Lines that carry content: trailing spaces and blank lines are ignored."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def line_delta(before: str, after: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(content_lines(before), content_lines(after), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def check_meaningful(before: str, after: str) -> tuple[bool, str]:
    """Decide whether a doc diff is worth a pull request."""
    if before == after:
        return False, "the README is unchanged"
    if content_lines(before) == content_lines(after):
        return False, "only whitespace or blank lines changed"
    if not after.strip():
        return False, "the generated README is empty"
    if len(before.strip()) > 2000 and len(after.strip()) * 2 < len(before.strip()):
        return False, "the generated README lost more than half its content; refusing to publish a rewrite"
    added, removed = line_delta(before, after)
    return True, f"{added} line(s) added, {removed} removed"


def sections_by_title(markdown: str) -> dict[str, dict]:
    """normalized title -> {title, start, end, text}."""
    lines = markdown.splitlines()
    found: dict[str, dict] = {}
    for section in docs_gen.parse_sections(markdown):
        found[docs_gen.normalize_title(section.title)] = {
            "title": section.title,
            "start": section.start,
            "end": section.end,
            "text": "\n".join(lines[section.start : section.end]).strip(),
        }
    return found


def changed_sections(before: str, after: str) -> list[tuple[str, str]]:
    """(display title, kind) for sections added, updated or removed.

    Parent headings are dropped when one of their subsections changed, so an H1
    that spans the whole README does not swallow every edit.
    """
    old = sections_by_title(before)
    new = sections_by_title(after)
    items: list[tuple[str, str, str, int, int, str]] = []
    for key, section in new.items():
        if key not in old:
            items.append((key, section["title"], "added", section["start"], section["end"], "new"))
        elif old[key]["text"] != section["text"]:
            items.append((key, section["title"], "updated", section["start"], section["end"], "new"))
    for key, section in old.items():
        if key not in new:
            items.append((key, section["title"], "removed", section["start"], section["end"], "old"))

    changes: list[tuple[str, str]] = []
    for key, title, kind, start, end, doc in items:
        nested = any(
            other_doc == doc and other_key != key and start < other_start < end
            for other_key, _, _, other_start, _, other_doc in items
        )
        if not nested:
            changes.append((title, kind))
    return changes


def heading_of(change: dict) -> str:
    text = change["markdown"].strip()
    first = text.splitlines()[0] if text else ""
    match = docs_gen.HEADING_RE.match(first)
    if match:
        return match.group(2).strip()
    return change.get("heading") or "section"


# --------------------------------------------------------------------------
# pull request plan
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishPlan:
    repo_name: str
    branch: str
    base: str
    path: str
    content: str
    commit_message: str
    pr_title: str
    pr_body: str


def pr_title_for(report: watcher.Report) -> str:
    subject = report.head.message.splitlines()[0] if report.head.message else report.head.sha
    return f"📝 Docs: auto-update for {subject[:120]}"


def render_body(
    report: watcher.Report,
    path: str,
    before: str,
    after: str,
    applied: list[dict],
    skipped: list[tuple[dict, str]],
    summary: str,
) -> str:
    subject = report.head.message.splitlines()[0] if report.head.message else ""
    lines = [f"Automated documentation update for `{report.head.sha}`.", ""]
    if summary:
        lines += [summary, ""]

    lines += [
        f"**Commit:** [{report.head.sha}]({report.head.url}) by {report.head.author}  ",
        f"**Message:** {subject}",
        "",
        "### Code changes",
    ]
    if report.files:
        lines += ["| File | Change | Lines |", "| --- | --- | --- |"]
        for change in report.files:
            lines.append(f"| `{change.filename}` | {change.status} | +{change.additions} / -{change.deletions} |")
    else:
        lines.append("_No file changes in this range._")

    lines += ["", f"### Docs changes in `{path}`"]
    entries: list[str] = []
    for change in applied:
        title = heading_of(change)
        if change["action"] == "replace":
            entry = f"- **{title}** section updated"
        else:
            anchor = change.get("after")
            entry = f"- **{title}** section added" + (f" after **{anchor}**" if anchor else " at the end")
        if change["reason"]:
            entry += f" - {change['reason']}"
        entries.append(entry)
    if not entries:
        for title, kind in changed_sections(before, after):
            entries.append(f"- **{title}** section {kind}")
    lines += entries or ["_No sections changed._"]

    if skipped:
        lines += ["", "<details><summary>Skipped model suggestions</summary>", ""]
        for change, reason in skipped:
            label = change.get("heading") or change.get("after") or "section"
            lines.append(f"- `{label}`: {reason}")
        lines += ["", "</details>"]

    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    lines += ["", "<details><summary>Doc diff</summary>", "", "~~~diff"]
    lines += diff[:MAX_DIFF_LINES]
    if len(diff) > MAX_DIFF_LINES:
        lines.append(f"... diff truncated at {MAX_DIFF_LINES} lines ...")
    lines += ["~~~", "", "</details>", "", "---", "_Generated by the docs agent. Review the wording before merging._"]

    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n_body truncated_"
    return body


def build_plan(
    repo_name: str,
    base: str,
    path: str,
    report: watcher.Report,
    before: str,
    after: str,
    applied: list[dict],
    skipped: list[tuple[dict, str]],
    summary: str,
) -> PublishPlan:
    return PublishPlan(
        repo_name=repo_name,
        branch=f"docs/auto-update-{report.head.sha}",
        base=base,
        path=path,
        content=after,
        commit_message=f"docs: auto-update for {report.head.sha}\n\n{report.head.url}",
        pr_title=pr_title_for(report),
        pr_body=render_body(report, path, before, after, applied, skipped, summary),
    )


# --------------------------------------------------------------------------
# GitHub writes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    branch: str
    commit_url: str | None
    pr_url: str | None
    pr_number: int | None
    pr_updated: bool = False


def ensure_branch(repo: Repository, branch: str, base: str) -> bool:
    """Create the branch if needed. Returns True when it already existed."""
    try:
        repo.get_branch(branch)
        return True
    except UnknownObjectException:
        base_sha = repo.get_branch(base).commit.sha
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
        return False


def current_content(repo: Repository, path: str, branch: str) -> ContentFile | None:
    try:
        existing = repo.get_contents(path, ref=branch)
    except UnknownObjectException:
        return None
    if isinstance(existing, list):
        raise PublishError(f"{path} is a directory in the repository")
    return existing


def commit_doc(repo: Repository, plan: PublishPlan) -> str | None:
    existing = current_content(repo, plan.path, plan.branch)
    if existing is not None:
        try:
            current = existing.decoded_content.decode("utf-8-sig").replace("\r\n", "\n")
        except (AttributeError, UnicodeDecodeError, AssertionError):
            # Large or base64-less files cannot be compared; just update them.
            current = ""
        if current == plan.content:
            return None
        result = repo.update_file(plan.path, plan.commit_message, plan.content, sha=existing.sha, branch=plan.branch)
    else:
        result = repo.create_file(plan.path, plan.commit_message, plan.content, branch=plan.branch)
    commit = result.get("commit") if isinstance(result, dict) else None
    return getattr(commit, "html_url", None)


def find_open_pr(repo: Repository, plan: PublishPlan):
    owner = repo.owner.login
    for pull in repo.get_pulls(state="open", head=f"{owner}:{plan.branch}", base=plan.base):
        return pull
    return None


def open_or_update_pr(repo: Repository, plan: PublishPlan, *, draft: bool):
    pull = find_open_pr(repo, plan)
    if pull is not None:
        pull.edit(title=plan.pr_title, body=plan.pr_body)
        return pull, True
    try:
        return repo.create_pull(
            title=plan.pr_title,
            body=plan.pr_body,
            head=plan.branch,
            base=plan.base,
            draft=draft,
        ), False
    except GithubException as exc:
        if exc.status == 422:
            raise PublishError(
                f"GitHub refused the pull request: {describe(exc)}. "
                "The branch may already be merged into the base branch."
            )
        raise


def publish(repo: Repository, plan: PublishPlan, *, open_pr: bool, draft: bool) -> PublishResult:
    existed = ensure_branch(repo, plan.branch, plan.base)
    if existed:
        warn(f"branch {plan.branch} already exists; committing on top of it")

    commit_url = commit_doc(repo, plan)
    if commit_url is None:
        print(paint(f"  {plan.branch} already contains this exact {plan.path}", "dim"))
    else:
        print(f"{stamp()} committed {paint(plan.path, 'bold')} to {paint(plan.branch, 'cyan')}")

    if not open_pr:
        return PublishResult(plan.branch, commit_url, None, None)

    pull, updated = open_or_update_pr(repo, plan, draft=draft)
    return PublishResult(plan.branch, commit_url, pull.html_url, pull.number, updated)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publisher.py",
        description="Commit regenerated docs to a branch and open a pull request.",
    )
    parser.add_argument("repo", nargs="?", help='repository as "owner/name" (or set GITHUB_REPO)')
    parser.add_argument(
        "--readme", default=docs_gen.DEFAULT_README, metavar="PATH", help=f"local README (default {docs_gen.DEFAULT_README})"
    )
    parser.add_argument("--path", metavar="PATH", help="path to commit in the repo (default: --readme)")
    parser.add_argument("--base", metavar="BRANCH", help="base branch for the diff and the PR (default: repo default)")
    parser.add_argument("--since", metavar="SHA", help="diff from this commit (default: head's first parent)")
    parser.add_argument("--until", metavar="REF", help="diff up to this ref (default: base branch head)")
    parser.add_argument("--from-file", metavar="PATH", help="publish this generated README instead of calling the LLM")
    parser.add_argument("--dry-run", action="store_true", help="print the branch/commit/PR plan without writing anything")
    parser.add_argument("--no-pr", action="store_true", help="push the branch but do not open a pull request")
    parser.add_argument("--draft", action="store_true", help="open the pull request as a draft")
    parser.add_argument("--model", metavar="NAME", help=f"model to use (default {docs_gen.DEFAULT_MODEL} or LLM_MODEL)")
    parser.add_argument("--base-url", metavar="URL", help="OpenAI-compatible endpoint (or LLM_BASE_URL)")
    parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=docs_gen.DEFAULT_MAX_DIFF_CHARS,
        metavar="N",
        help=f"diff characters sent to the model (default {docs_gen.DEFAULT_MAX_DIFF_CHARS})",
    )
    return parser.parse_args(argv)


def repo_path_for(args: argparse.Namespace) -> str:
    if args.path:
        return args.path.replace("\\", "/")
    local = Path(args.readme)
    try:
        # Commit the path the file has inside the checkout, not an absolute
        # Windows path, which GitHub would happily accept as a new filename.
        return local.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return local.name


def generate_merged(
    args: argparse.Namespace,
    repo_name: str,
    base: str,
    report: watcher.Report,
    readme: str,
) -> tuple[str, list[dict], list[tuple[dict, str]], str]:
    """Run the Part 2 pipeline: prompt, call, validate, splice."""
    config = docs_gen.load_llm_config(argparse.Namespace(model=args.model, base_url=args.base_url))
    if config is None:
        raise PublishError("missing LLM API key. Set LLM_API_KEY (or OPENAI_API_KEY) in .env", exit_code=2)

    messages = docs_gen.build_messages(report, readme, repo_name, base, args.max_diff_chars)
    data, tokens = docs_gen.call_llm(config, messages)
    changes = docs_gen.normalize_changes(data)
    print(f"{stamp()} {paint(config.model, 'bold')}  {paint(f'{tokens:,} tokens', 'dim')}")

    merged, applied, skipped = docs_gen.merge_readme(readme, changes)
    return merged, applied, skipped, str(data.get("summary") or "").strip()


def read_generated(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    except FileNotFoundError:
        raise PublishError(f"generated README not found: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise PublishError(f"cannot read {path}: {exc}")


def print_plan(plan: PublishPlan) -> None:
    print()
    print(paint(f"dry run for {plan.repo_name}", "bold"))
    print(f"  branch   {plan.branch}")
    print(f"  base     {plan.base}")
    print(f"  file     {plan.path}")
    print(f"  commit   {plan.commit_message.splitlines()[0]}")
    print(f"  title    {plan.pr_title}")
    print()
    print(paint("--- pull request body ---", "dim"))
    print(plan.pr_body)


def main(argv: list[str] | None = None) -> int:
    docs_gen.configure_output()
    load_dotenv(ENV_FILE)
    args = parse_args(argv)

    repo_name = (args.repo or os.environ.get("GITHUB_REPO", "")).strip()
    if not repo_name:
        error("no repository given. Pass one as an argument or set GITHUB_REPO in .env")
        return 2
    if args.max_diff_chars < 1:
        error("--max-diff-chars must be at least 1")
        return 2

    readme_path = Path(args.readme)
    loaded = docs_gen.load_readme(readme_path)
    if loaded is None:
        return 2
    readme, _newline = loaded
    path = repo_path_for(args)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        warn("GITHUB_TOKEN is missing; using anonymous access (60 requests/hour)")

    github = Github(auth=Auth.Token(token) if token else None, per_page=100, retry=0)
    try:
        repo = github.get_repo(repo_name)
        base = args.base or repo.default_branch
        head: Commit = repo.get_commit(sha=args.until or base)
        report = watcher.build_report(repo, head, args.since)
    except RateLimitExceededException:
        error("GitHub API rate limit exceeded; wait for the reset or set GITHUB_TOKEN in .env")
        return 2
    except GithubException as exc:
        hint = " (rate limit)" if exc.status == 403 else ""
        error(f"GitHub error: {describe(exc)}{hint}")
        return 2
    except requests.RequestException as exc:
        error(f"network error: {exc}")
        return 2
    finally:
        github.close()

    try:
        if args.from_file:
            merged = read_generated(Path(args.from_file))
            applied: list[dict] = []
            skipped: list[tuple[dict, str]] = []
            summary = ""
        else:
            merged, applied, skipped, summary = generate_merged(args, repo_name, base, report, readme)
    except docs_gen.LLMError as exc:
        error(str(exc))
        return exc.exit_code
    except requests.RequestException as exc:
        error(f"cannot reach the LLM endpoint: {exc}")
        return 1
    except ValueError as exc:
        error(f"unusable model output: {exc}")
        return 1
    except PublishError as exc:
        error(str(exc))
        return exc.exit_code

    meaningful, reason = check_meaningful(readme, merged)
    if not meaningful:
        print()
        print(paint(f"  no pull request: {reason}", "dim"))
        return 0

    plan = build_plan(repo_name, base, path, report, readme, merged, applied, skipped, summary)
    print()
    print(f"{stamp()} docs changed: {paint(f'{len(merged.splitlines())} lines', 'bold')} ({reason})")

    if args.dry_run:
        print_plan(plan)
        return 0

    github = Github(auth=Auth.Token(token) if token else None, per_page=100, retry=0)
    try:
        repo = github.get_repo(repo_name)
        result = publish(repo, plan, open_pr=not args.no_pr, draft=args.draft)
    except PublishError as exc:
        error(str(exc))
        return exc.exit_code
    except GithubException as exc:
        error(f"GitHub error: {describe(exc)}")
        return 1
    except requests.RequestException as exc:
        error(f"network error: {exc}")
        return 1
    finally:
        github.close()

    print()
    print(f"  branch   {paint(result.branch, 'cyan')}")
    if result.commit_url:
        print(f"  commit   {result.commit_url}")
    if result.pr_url:
        verb = "updated" if result.pr_updated else "opened"
        print(f"  PR       {verb} #{result.pr_number} {paint(result.pr_url, 'blue')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())