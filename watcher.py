"""Watch a GitHub repository and print a summary of each new commit's diff.

Part 1 of the docs agent: a polling watcher that reports what changed so a
later stage can decide which documentation needs updating.

Usage:
    python watcher.py owner/repo
    python watcher.py owner/repo --branch main --interval 30
    python watcher.py owner/repo --once --full-patch

Configuration (.env next to this file, or real environment variables):
    GITHUB_TOKEN   personal access token with read access to the repo
    GITHUB_REPO    "owner/repo" when it is not passed on the command line

The repository is polled by fetching the branch head (one API request per
poll). A diff is only requested when the head SHA has changed.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from github import Auth, Github, GithubException
from github.Commit import Commit
from github.File import File
from github.Repository import Repository

ENV_FILE = Path(__file__).with_name(".env")

DEFAULT_INTERVAL = 30
DEFAULT_MAX_PATCH_LINES = 60

# GitHub caps these collections, so a "complete" diff can silently be short.
MAX_COMPARE_FILES = 300
MAX_COMPARE_COMMITS = 250

STATUS_LABEL = {
    "added": "A",
    "removed": "D",
    "modified": "M",
    "renamed": "R",
    "copied": "C",
    "changed": "M",
    "unchanged": "=",
}

STATUS_COLOR = {
    "added": "green",
    "removed": "red",
    "modified": "yellow",
    "renamed": "cyan",
    "copied": "cyan",
    "changed": "yellow",
    "unchanged": "dim",
}

ANSI = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}

_USE_COLOR = False


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------


def init_color() -> bool:
    """Enable ANSI colors when writing to a real terminal (Windows included)."""
    if os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False
    return True


def paint(text: str, *styles: str) -> str:
    if not _USE_COLOR or not styles:
        return text
    codes = ";".join(ANSI[s] for s in styles if s in ANSI)
    return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


def configure_stdout() -> None:
    """Keep commit messages with emoji or CJK text from crashing cp1252 pipes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def stamp() -> str:
    return paint(f"[{datetime.now():%H:%M:%S}]", "dim")


def warn(message: str) -> None:
    print(f"{stamp()} {paint('!', 'yellow', 'bold')} {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"{paint('error:', 'red', 'bold')} {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileChange:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_filename: str | None = None

    @classmethod
    def from_api(cls, f: File) -> "FileChange":
        return cls(
            filename=f.filename,
            status=f.status,
            additions=f.additions or 0,
            deletions=f.deletions or 0,
            patch=getattr(f, "patch", None),
            previous_filename=getattr(f, "previous_filename", None),
        )


@dataclass(frozen=True)
class CommitSummary:
    sha: str
    message: str
    author: str
    date: str
    url: str


@dataclass
class Report:
    head: CommitSummary
    base_sha: str | None
    files: list[FileChange]
    commits: list[CommitSummary]
    notes: list[str]

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def summarize_commit(commit: Commit) -> CommitSummary:
    info = commit.commit
    raw_author = info.author
    name = raw_author.name if raw_author and raw_author.name else "unknown"
    login = commit.author.login if commit.author is not None else None
    author = f"{name} (@{login})" if login else name
    date = raw_author.date.strftime("%Y-%m-%d %H:%M UTC") if raw_author and raw_author.date else "unknown date"
    return CommitSummary(
        sha=commit.sha[:7],
        message=(info.message or "").strip(),
        author=author,
        date=date,
        url=commit.html_url,
    )


def fetch_head(repo: Repository, branch: str) -> Commit:
    """One API request per poll: GET /repos/{owner}/{repo}/commits/{ref}."""
    return repo.get_commit(sha=branch)


def build_report(repo: Repository, head: Commit, last_sha: str | None) -> Report:
    """Diff `last_sha..head`, or the head commit itself on first sight."""
    head_info = summarize_commit(head)
    notes: list[str] = []
    base_sha = last_sha

    if base_sha is None:
        if not head.parents:
            return Report(
                head=head_info,
                base_sha=None,
                files=[FileChange.from_api(f) for f in head.files],
                commits=[head_info],
                notes=["initial commit: no parent to diff against"],
            )
        base_sha = head.parents[0].sha
        notes.append("baseline: diffing the latest commit against its first parent")

    try:
        comparison = repo.compare(base_sha, head.sha)
    except GithubException as exc:
        # Force-push or rebase: the old tip is gone, so show the head commit only.
        if exc.status != 404 or not head.parents:
            raise
        base_sha = head.parents[0].sha
        comparison = repo.compare(base_sha, head.sha)
        notes.append("previous commit unreachable (force-push?); showing the latest commit only")

    files = [FileChange.from_api(f) for f in comparison.files]
    raw_commits = list(comparison.commits)
    commits = [summarize_commit(c) for c in raw_commits if c.sha != head.sha]
    commits.append(head_info)

    if len(comparison.files) >= MAX_COMPARE_FILES:
        notes.append(f"file list truncated by GitHub at {MAX_COMPARE_FILES} files")
    if len(raw_commits) >= MAX_COMPARE_COMMITS:
        notes.append(f"commit list truncated by GitHub at {MAX_COMPARE_COMMITS} commits")

    return Report(head=head_info, base_sha=base_sha, files=files, commits=commits, notes=notes)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def short_path(path: str, width: int = 48) -> str:
    return path if len(path) <= width else "..." + path[-width + 3 :]


def color_patch_line(line: str) -> str:
    if line.startswith("+++") or line.startswith("---"):
        return paint(line, "dim")
    if line.startswith("+"):
        return paint(line, "green")
    if line.startswith("-"):
        return paint(line, "red")
    if line.startswith("@@"):
        return paint(line, "cyan")
    if line.startswith(("diff ", "index ", "new file", "deleted file", "similarity ", "rename ")):
        return paint(line, "dim")
    return line


def print_patch(change: FileChange, *, full_patch: bool, max_lines: int) -> None:
    print(paint(f"  --- {change.filename} ---", "dim"))
    if not change.patch:
        print(paint("  (no patch: binary file, mode change, or diff too large)", "dim"))
        return
    lines = change.patch.splitlines()
    shown = lines if full_patch else lines[:max_lines]
    for line in shown:
        print("  " + color_patch_line(line))
    hidden = len(lines) - len(shown)
    if hidden > 0:
        print(paint(f"  ... {hidden} more line{'s' if hidden != 1 else ''} (use --full-patch)", "dim"))


def print_report(report: Report, *, full_patch: bool, max_lines: int) -> None:
    head = report.head
    print()
    print(f"{stamp()} {paint('commit ' + head.sha, 'bold')}  {paint(head.date, 'dim')}")
    print(f"  author   {head.author}")

    subject, _, body = head.message.partition("\n")
    print(f"  message  {subject}")
    for line in body.strip().splitlines()[:10]:
        print(f"           {line}")
    print(f"  url      {paint(head.url, 'blue')}")

    for note in report.notes:
        print(paint(f"  note     {note}", "dim"))

    extra = report.commits[:-1]
    if extra:
        print()
        print(paint(f"  {len(extra) + 1} commits since {report.base_sha[:7]}:", "bold"))
        for commit in [*extra, head]:
            line = f"    {commit.sha}  {commit.message.splitlines()[0] if commit.message else ''}"
            print(line if len(line) <= 100 else line[:97] + "...")

    print()
    if not report.files:
        print(paint("  no file changes (empty commit)", "dim"))
        return

    print(
        f"  {paint(str(len(report.files)) + ' file' + ('s' if len(report.files) != 1 else '') + ' changed', 'bold')}"
        f"  {paint(f'+{report.additions}', 'green')} {paint(f'-{report.deletions}', 'red')}"
    )
    print()
    for change in report.files:
        label = STATUS_LABEL.get(change.status, "?")
        # Pad before painting so escape codes don't break column alignment.
        plus = f"+{change.additions}".rjust(6)
        minus = f"-{change.deletions}".rjust(6)
        row = (
            f"  {paint(label, STATUS_COLOR.get(change.status, 'dim'), 'bold')}  "
            f"{short_path(change.filename):<48} {paint(plus, 'green')} {paint(minus, 'red')}"
        )
        print(row)
        if change.previous_filename:
            print(paint(f"       from {change.previous_filename}", "dim"))

    print()
    for change in report.files:
        print_patch(change, full_patch=full_patch, max_lines=max_lines)
        print()


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def describe(exc: GithubException) -> str:
    data = exc.data if isinstance(exc.data, dict) else {}
    message = data.get("message")
    return f"{message} (HTTP {exc.status})" if message and exc.status else (message or str(exc))


def parse_args(argv: list[str] | None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="watcher.py",
        description="Poll a GitHub repository and print a summary of each new commit.",
    )
    parser.add_argument("repo", nargs="?", help='repository as "owner/name" (or set GITHUB_REPO)')
    parser.add_argument("-b", "--branch", help="branch to watch (default: repository default branch)")
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"seconds between polls (default {DEFAULT_INTERVAL})",
    )
    parser.add_argument("--once", action="store_true", help="print the latest commit diff and exit")
    parser.add_argument("--full-patch", action="store_true", help="print complete patches instead of a sample")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_PATCH_LINES,
        metavar="N",
        help=f"patch lines printed per file (default {DEFAULT_MAX_PATCH_LINES})",
    )
    return parser, parser.parse_args(argv)


def watch(repo: Repository, branch: str, args: argparse.Namespace) -> int:
    last_sha: str | None = None
    if args.once:
        print(paint(f"Checking {repo.full_name} ({branch}) once.", "bold"))
    else:
        print(paint(f"Watching {repo.full_name} ({branch}) - polling every {args.interval}s. Ctrl+C to stop.", "bold"))

    while True:
        try:
            head = fetch_head(repo, branch)
        except GithubException as exc:
            detail = (exc.data.get("message") if isinstance(exc.data, dict) else "") or ""
            if exc.status == 403 and "rate limit" in detail.lower():
                warn(f"API rate limit hit; retrying in {args.interval}s")
            elif exc.status in (401, 403, 404):
                error(f"cannot read {repo.full_name}@{branch}: {describe(exc)}")
                return 2
            else:
                warn(f"GitHub error while polling: {describe(exc)}")
        except requests.RequestException as exc:
            warn(f"network error while polling: {exc}")
        else:
            if head.sha != last_sha:
                try:
                    report = build_report(repo, head, last_sha)
                except GithubException as exc:
                    warn(f"could not build diff for {head.sha[:7]}: {describe(exc)}")
                except requests.RequestException as exc:
                    warn(f"network error while diffing: {exc}")
                else:
                    print_report(report, full_patch=args.full_patch, max_lines=args.max_lines)
                    sys.stdout.flush()
                    last_sha = head.sha

        if args.once:
            return 0
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    global _USE_COLOR
    configure_stdout()
    _USE_COLOR = init_color()
    load_dotenv(ENV_FILE)
    parser, args = parse_args(argv)

    repo_name = (args.repo or os.environ.get("GITHUB_REPO", "")).strip()
    if not repo_name:
        parser.print_usage(sys.stderr)
        error("no repository given. Pass one as an argument or set GITHUB_REPO in .env")
        return 2

    if args.interval < 1:
        error("--interval must be at least 1 second")
        return 2
    if args.max_lines < 1:
        error("--max-lines must be at least 1")
        return 2

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        warn("GITHUB_TOKEN is missing; using anonymous access (60 requests/hour)")

    github = Github(auth=Auth.Token(token) if token else None, per_page=100)
    try:
        repo = github.get_repo(repo_name)
    except GithubException as exc:
        hint = " - check the token in .env" if exc.status == 401 else ""
        error(f"cannot access {repo_name}: {describe(exc)}{hint}")
        return 2
    except requests.RequestException as exc:
        error(f"network error: {exc}")
        return 2

    branch = args.branch or repo.default_branch
    try:
        return watch(repo, branch, args)
    except KeyboardInterrupt:
        print(paint("\nStopped.", "dim"))
        return 0
    finally:
        github.close()


if __name__ == "__main__":
    raise SystemExit(main())