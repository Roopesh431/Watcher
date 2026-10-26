"""Run the docs agent: poll GitHub, update docs, open a pull request.

Part 4 glues the three stages together:

    watcher.py    fetch the new commit and its diff
    docs_gen.py   ask the LLM for the affected README sections and splice them in
    publisher.py  commit to docs/auto-update-<sha> and open the PR

Usage:
    python main.py watch
    python main.py watch owner/repo --interval 15
    python main.py once
    python main.py watch --dry-run

The last processed commit is remembered in .docs-agent-state.json, so restarting
the loop does not repeat work and commits made while it was down are still
picked up. Nothing is pushed when the docs did not meaningfully change.

Configuration (.env next to this file, or real environment variables):
    GITHUB_TOKEN, GITHUB_REPO      repository access
    LLM_API_KEY / OPENAI_API_KEY   docs generation
    LLM_MODEL, LLM_BASE_URL        optional model overrides
    POLL_INTERVAL                  default poll interval in seconds (30)
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from github import Auth, Github, GithubException, RateLimitExceededException
from github.Commit import Commit
from github.Repository import Repository

import docs_gen
import publisher
import watcher

ENV_FILE = watcher.ENV_FILE
STATE_FILE = Path(".docs-agent-state.json")
DEFAULT_INTERVAL = 30
MAX_ATTEMPTS = 3

paint = watcher.paint
stamp = watcher.stamp
warn = watcher.warn
error = watcher.error
describe = watcher.describe


class RateLimited(Exception):
    """A stage hit an API rate limit; wait, then retry the same commit."""

    def __init__(self, wait_seconds: float, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.wait_seconds = wait_seconds
        self.source = source
        self.message = message


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# rate limits and state
# --------------------------------------------------------------------------


def header_value(headers, name: str) -> str | None:
    if not headers:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    wanted = name.casefold()
    for key, value in items:
        if str(key).casefold() == wanted:
            return str(value)
    return None


def rate_limit_wait(exc: GithubException) -> float | None:
    """Seconds to wait when the failure is a GitHub rate limit, else None."""
    headers = getattr(exc, "headers", None) or {}
    message = ""
    if isinstance(getattr(exc, "data", None), dict):
        message = str(exc.data.get("message") or "")
    status = getattr(exc, "status", None)
    exhausted = header_value(headers, "x-ratelimit-remaining") == "0"
    if status not in (403, 429) or not (exhausted or "rate limit" in message.lower()):
        return None
    reset = header_value(headers, "x-ratelimit-reset")
    try:
        wait = int(reset) - time.time() + 5 if reset else 60.0
    except (TypeError, ValueError):
        wait = 60.0
    return max(5.0, min(float(wait), 3600.0))


def github_call(fn, *args, **kwargs):
    """Run a GitHub call, converting rate limits into RateLimited."""
    try:
        return fn(*args, **kwargs)
    except RateLimitExceededException as exc:
        raise RateLimited(rate_limit_wait(exc) or 60.0, "GitHub", describe(exc))
    except GithubException as exc:
        wait = rate_limit_wait(exc)
        if wait is not None:
            raise RateLimited(wait, "GitHub", describe(exc))
        raise


def wait_out(exc: RateLimited) -> None:
    until = datetime.now() + timedelta(seconds=exc.wait_seconds)
    print()
    print(f"{stamp()} {paint('rate limited', 'yellow', 'bold')} ({exc.source}): {exc.message}")
    print(paint(f"  sleeping {int(exc.wait_seconds)}s until {until:%H:%M:%S} - Ctrl+C to stop", "dim"))
    time.sleep(exc.wait_seconds)


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"cannot write state to {path}: {exc}")


def state_key(repo_name: str, base: str) -> str:
    return f"{repo_name.casefold()}@{base}"


# --------------------------------------------------------------------------
# one commit
# --------------------------------------------------------------------------


def read_readme(
    repo: Repository,
    path: str,
    base: str,
    local: str,
    local_path: Path,
) -> tuple[str, str]:
    """Prefer the README on the base branch; fall back to the local file."""
    try:
        content = github_call(repo.get_contents, path, ref=base)
    except GithubException as exc:
        if exc.status != 404:
            raise
        content = None
    if isinstance(content, list):
        raise ConfigError(f"{path} is a directory in the repository")
    if content is not None:
        try:
            return content.decoded_content.decode("utf-8-sig").replace("\r\n", "\n"), "branch"
        except (AttributeError, UnicodeDecodeError, AssertionError):
            warn(f"cannot decode {path} from {base}; using the local copy")
    warn(f"{path} is not on {base}; using the local copy at {local_path}")
    return local, "local"


def process_commit(
    repo: Repository,
    repo_name: str,
    base: str,
    path: str,
    local_readme: str,
    local_path: Path,
    head: Commit,
    since: str | None,
    config: docs_gen.LLMConfig,
    args: argparse.Namespace,
) -> str:
    """Generate and publish the docs update for one commit. Returns a status."""
    report = github_call(watcher.build_report, repo, head, since)
    if not report.files:
        print(paint("  no file changes; nothing to document", "dim"))
        return "empty"

    readme, origin = read_readme(repo, path, base, local_readme, local_path)
    print(f"  source   {path} from {origin} ({len(readme.splitlines())} lines)")

    messages = docs_gen.build_messages(report, readme, repo_name, base, args.max_diff_chars)
    try:
        data, tokens = docs_gen.call_llm(config, messages)
    except docs_gen.LLMError as exc:
        if "rate limit" in str(exc).lower():
            raise RateLimited(60.0, "LLM", str(exc))
        raise
    changes = docs_gen.normalize_changes(data)
    merged, applied, skipped = docs_gen.merge_readme(readme, changes)
    summary = str(data.get("summary") or "").strip()

    print(f"{stamp()} {paint(config.model, 'bold')}  {paint(f'{tokens:,} tokens', 'dim')}")
    if summary:
        print(f"  impact   {summary}")
    for change, reason in skipped:
        print(paint(f"  skipped: {publisher.describe_change(change)} ({reason})", "yellow"))

    meaningful, reason = publisher.check_meaningful(readme, merged)
    if not meaningful:
        print(paint(f"  no pull request: {reason}", "dim"))
        return "unchanged"

    plan = publisher.build_plan(repo_name, base, path, report, readme, merged, applied, skipped, summary)
    print(f"  docs     {len(merged.splitlines())} lines ({reason})")

    if args.dry_run:
        publisher.print_plan(plan)
        return "dry-run"

    result = github_call(publisher.publish, repo, plan, open_pr=not args.no_pr, draft=args.draft)
    if result.pr_url:
        verb = "updated" if result.pr_updated else "opened"
        print(f"{stamp()} PR {verb} #{result.pr_number} {paint(result.pr_url, 'blue')}")
    else:
        print(f"{stamp()} branch {paint(result.branch, 'cyan')} pushed (no PR requested)")
    return "published"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def open_repo(token: str, repo_name: str) -> tuple[Github, Repository]:
    github = Github(auth=Auth.Token(token) if token else None, per_page=100, retry=0)
    try:
        repo = github_call(github.get_repo, repo_name)
    except BaseException:
        github.close()
        raise
    return github, repo


def open_repo_waiting(token: str, repo_name: str) -> tuple[Github, Repository]:
    """Open the repo, parking until the rate limit resets if we start limited."""
    while True:
        try:
            return open_repo(token, repo_name)
        except RateLimited as exc:
            wait_out(exc)


def load_inputs(args: argparse.Namespace) -> tuple[str, str, docs_gen.LLMConfig, str, Path]:
    """Validate config and load the local README before touching the network."""
    repo_name = (args.repo or os.environ.get("GITHUB_REPO", "")).strip()
    if not repo_name:
        raise ConfigError("no repository given. Pass one as an argument or set GITHUB_REPO in .env")
    if args.max_diff_chars < 1:
        raise ConfigError("--max-diff-chars must be at least 1")

    config = docs_gen.load_llm_config(argparse.Namespace(model=args.model, base_url=args.base_url))
    if config is None:
        raise ConfigError("missing LLM API key. Set LLM_API_KEY (or OPENAI_API_KEY) in .env")

    local_path = Path(args.readme)
    loaded = docs_gen.load_readme(local_path)
    if loaded is None:
        raise ConfigError(f"local README not found: {local_path}")
    local_readme, _newline = loaded
    path = publisher.repo_path_for(args)
    return repo_name, path, config, local_readme, local_path


def watch(args: argparse.Namespace) -> int:
    repo_name, path, config, local_readme, local_path = load_inputs(args)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        warn("GITHUB_TOKEN is missing; using anonymous access (60 requests/hour)")

    github, repo = open_repo_waiting(token, repo_name)
    try:
        base = args.base or repo.default_branch
        key = state_key(repo_name, base)
        state_path = Path(args.state)
        state = {} if args.no_state else load_state(state_path)

        while True:
            try:
                head = github_call(watcher.fetch_head, repo, base)
                break
            except RateLimited as exc:
                wait_out(exc)

        last_sha = args.since or state.get(key, {}).get("last_sha")
        if not last_sha:
            last_sha = head.sha
            print(f"{stamp()} baseline {paint(head.sha[:7], 'bold')} - waiting for new commits")
            if not args.no_state:
                state[key] = {"last_sha": last_sha, "updated": datetime.now().isoformat(timespec="seconds")}
                save_state(state_path, state)
        elif head.sha != last_sha:
            print(f"{stamp()} head {paint(head.sha[:7], 'bold')} is ahead of {last_sha[:7]}; catching up")

        print(paint(f"Watching {repo_name} ({base}) every {args.interval}s - Ctrl+C to stop.", "bold"))

        poll = 0
        attempts = 0
        while True:
            poll += 1
            if args.max_iterations and poll > args.max_iterations:
                return 0
            try:
                head = github_call(watcher.fetch_head, repo, base)
            except RateLimited as exc:
                wait_out(exc)
                continue
            except (GithubException, requests.RequestException) as exc:
                warn(f"poll failed: {exc}")
                time.sleep(min(5 * poll, args.interval))
                continue

            if head.sha == last_sha:
                print(paint(f"{stamp()} no changes (head {head.sha[:7]})", "dim"))
            else:
                print()
                print(f"{stamp()} new commit {paint(head.sha[:7], 'bold')} ({head.commit.message.splitlines()[0][:70]})")
                try:
                    process_commit(
                        repo, repo_name, base, path, local_readme, local_path, head, last_sha, config, args
                    )
                except RateLimited as exc:
                    wait_out(exc)
                    continue
                except (GithubException, requests.RequestException, docs_gen.LLMError, publisher.PublishError) as exc:
                    attempts += 1
                    warn(f"attempt {attempts}/{MAX_ATTEMPTS} failed for {head.sha[:7]}: {exc}")
                    if attempts >= MAX_ATTEMPTS:
                        warn(f"giving up on {head.sha[:7]}; moving to the next commit")
                    else:
                        time.sleep(min(5 * attempts, 30))
                        continue
                else:
                    attempts = 0

                last_sha = head.sha
                # Dry runs advance in memory only, so a real run still picks
                # this commit up.
                if not args.no_state and not args.dry_run:
                    state[key] = {
                        "last_sha": last_sha,
                        "updated": datetime.now().isoformat(timespec="seconds"),
                    }
                    save_state(state_path, state)

            time.sleep(args.interval)
    finally:
        github.close()


def once(args: argparse.Namespace) -> int:
    repo_name, path, config, local_readme, local_path = load_inputs(args)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        warn("GITHUB_TOKEN is missing; using anonymous access (60 requests/hour)")

    github, repo = open_repo(token, repo_name)
    try:
        base = args.base or repo.default_branch
        head = github_call(watcher.fetch_head, repo, base)
        print(paint(f"Checking {repo_name} ({base}) at {head.sha[:7]}", "bold"))
        status = process_commit(
            repo, repo_name, base, path, local_readme, local_path, head, args.since, config, args
        )
        print(paint(f"  status: {status}", "dim"))
        return 0
    finally:
        github.close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo", nargs="?", help='repository as "owner/name" (or set GITHUB_REPO)')
    parser.add_argument("--base", metavar="BRANCH", help="branch to watch and target (default: repo default)")
    parser.add_argument(
        "--readme", default=docs_gen.DEFAULT_README, metavar="PATH", help=f"local README (default {docs_gen.DEFAULT_README})"
    )
    parser.add_argument("--path", metavar="PATH", help="path of the README in the repository (default: --readme)")
    parser.add_argument("--since", metavar="SHA", help="diff from this commit (default: last processed commit)")
    parser.add_argument("--dry-run", action="store_true", help="generate and print the plan without writing anything")
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
    parser.add_argument("--state", default=str(STATE_FILE), metavar="PATH", help=f"state file (default {STATE_FILE})")
    parser.add_argument("--no-state", action="store_true", help="do not read or write the state file")


def env_interval() -> int:
    try:
        value = int(os.environ.get("POLL_INTERVAL", DEFAULT_INTERVAL))
    except ValueError:
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Poll a repository, regenerate the docs for new commits and open a pull request.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    watch_parser = sub.add_parser("watch", help="poll for new commits and publish docs")
    add_common(watch_parser)
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=env_interval(),
        metavar="SECONDS",
        help=f"seconds between polls (default {DEFAULT_INTERVAL} or POLL_INTERVAL)",
    )
    watch_parser.add_argument(
        "--max-iterations", type=int, default=0, metavar="N", help="stop after N polls (default: run forever)"
    )

    once_parser = sub.add_parser("once", help="process the current head commit and exit")
    add_common(once_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    docs_gen.configure_output()
    load_dotenv(ENV_FILE)
    args = build_parser().parse_args(argv)

    if getattr(args, "interval", 1) < 1:
        error("--interval must be at least 1 second")
        return 2
    if args.max_diff_chars < 1:
        error("--max-diff-chars must be at least 1")
        return 2

    try:
        return watch(args) if args.command == "watch" else once(args)
    except ConfigError as exc:
        error(str(exc))
        return 2
    except RateLimited as exc:
        until = datetime.now() + timedelta(seconds=exc.wait_seconds)
        error(f"{exc.source} rate limit: {exc.message}")
        print(paint(f"  retry after {until:%H:%M:%S} (about {int(exc.wait_seconds)}s)", "dim"))
        return 1
    except GithubException as exc:
        error(f"GitHub error: {describe(exc)}")
        return 1
    except requests.RequestException as exc:
        error(f"network error: {exc}")
        return 1
    except KeyboardInterrupt:
        print(paint("\nStopped.", "dim"))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())