"""Turn a commit diff into targeted README updates with an LLM.

Part 2 of the docs agent: watcher.py reports what changed; this module asks an
LLM to update only the affected documentation sections, then splices those
sections back into the existing README. Everything the model does not touch is
preserved exactly, so the current README stays the source of truth.

Usage:
    python docs_gen.py owner/repo
    python docs_gen.py owner/repo --since <sha> --readme docs/README.md
    python docs_gen.py owner/repo --write
    python docs_gen.py owner/repo --dry-run

Configuration (.env next to this file, or real environment variables):
    LLM_API_KEY / OPENAI_API_KEY   API key for an OpenAI-compatible endpoint
    LLM_MODEL                      model name (default: gpt-4o-mini)
    LLM_BASE_URL / OPENAI_BASE_URL endpoint (default: https://api.openai.com/v1)
    GITHUB_TOKEN, GITHUB_REPO      shared with watcher.py
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv
from github import Auth, Github, GithubException, RateLimitExceededException
from github.Commit import Commit

import watcher
from watcher import Report

ENV_FILE = watcher.ENV_FILE

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MAX_DIFF_CHARS = 60_000
DEFAULT_README = "README.md"

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

ACTION_ALIASES = {
    "replace": "replace",
    "update": "replace",
    "edit": "replace",
    "modify": "replace",
    "append_after": "append_after",
    "append": "append_after",
    "insert": "append_after",
    "insert_after": "append_after",
    "new": "append_after",
    "add": "append_after",
}

SYSTEM_PROMPT = """\
You are a senior technical writer maintaining a project's README.

You never rewrite a whole README. You update only the sections a code diff
actually affects, and you return the exact replacement markdown for those
sections. The current README is the source of truth: keep its wording,
structure, ordering, voice and formatting wherever the diff does not make
them wrong.

Rules:
- Consider only documentation that the diff supports: install/setup, usage,
  configuration and environment variables, API endpoints/reference, changelog.
- Do not invent features, flags, endpoints, files, versions or behaviour that
  the diff does not show. If the diff adds no user-visible change, return an
  empty changes list.
- Do not delete or rewrite subsections you are not changing.
- Each replacement must start with the section heading line, at the same
  heading level the README already uses.
- Changelog: keep existing entries and add the new entry at the top, newest
  first. Reference the commit sha; never invent release numbers or dates.
- Prefer short, concrete sentences and fenced code blocks for commands.
- Output JSON only, with no prose before or after it.

JSON schema:
{
  "summary": "under 20 words describing the documentation impact",
  "changes": [
    {
      "heading": "existing heading text without the # marks",
      "action": "replace",
      "markdown": "full replacement section, starting with the heading line",
      "reason": "why this section changed"
    },
    {
      "heading": null,
      "action": "append_after",
      "after": "existing heading text to insert after, or null for end of file",
      "markdown": "new section, starting with its heading line",
      "reason": "why this section is needed"
    }
  ]
}
"""


# --------------------------------------------------------------------------
# terminal output (watcher owns the ANSI setup; reuse it)
# --------------------------------------------------------------------------


def configure_output() -> None:
    watcher.configure_stdout()
    # watcher keeps its colour flag in a module global; use the same one so
    # there is a single ANSI implementation for both stages.
    watcher._USE_COLOR = watcher.init_color()


paint = watcher.paint
stamp = watcher.stamp
warn = watcher.warn
error = watcher.error
describe = watcher.describe


# --------------------------------------------------------------------------
# README structure
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    start: int  # index of the heading line
    end: int  # index of the next heading at the same or higher level
    level: int
    title: str


def normalize_title(title: str) -> str:
    text = title.strip().lstrip("#").strip().rstrip(":").strip()
    return re.sub(r"\s+", " ", text).casefold()


def heading_positions(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) for ATX headings outside fenced code."""
    found: list[tuple[int, int, str]] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        heading_match = HEADING_RE.match(line)
        if heading_match:
            found.append((index, len(heading_match.group(1)), heading_match.group(2).strip()))
    return found


def parse_sections(markdown: str) -> list[Section]:
    lines = markdown.splitlines()
    positions = heading_positions(lines)
    sections: list[Section] = []
    for order, (line_no, level, title) in enumerate(positions):
        end = len(lines)
        for later_no, later_level, _ in positions[order + 1 :]:
            if later_level <= level:
                end = later_no
                break
        sections.append(Section(start=line_no, end=end, level=level, title=title))
    return sections


def find_section(markdown: str, title: str) -> Section | None:
    wanted = normalize_title(title)
    for section in parse_sections(markdown):
        if normalize_title(section.title) == wanted:
            return section
    return None


def outline(markdown: str) -> str:
    sections = [s for s in parse_sections(markdown) if s.level <= 3]
    return "\n".join(f"- {'#' * s.level} {s.title}" for s in sections)


# --------------------------------------------------------------------------
# merging model output into the README
# --------------------------------------------------------------------------


class ApplyError(Exception):
    """A single proposed change could not be applied safely."""


def section_block(markdown_text: str, heading_hint: str | None) -> list[str]:
    lines = markdown_text.strip("\n").splitlines()
    if not lines:
        raise ApplyError("empty section markdown")
    if not HEADING_RE.match(lines[0]):
        if not heading_hint:
            raise ApplyError("section markdown does not start with a heading")
        lines.insert(0, heading_hint)
    return lines


def splice(lines: list[str], start: int, end: int, block: list[str]) -> str:
    """Replace lines[start:end] with block, keeping one blank line of spacing."""
    head = lines[:start]
    while head and not head[-1].strip():
        head.pop()
    tail = lines[end:]
    merged = list(head)
    if merged:
        merged.append("")
    merged.extend(block)
    if tail:
        merged.append("")
        merged.extend(tail)
    return "\n".join(merged).rstrip() + "\n"


def apply_change(markdown: str, change: dict) -> str:
    action = change["action"]
    lines = markdown.splitlines()
    if action == "replace":
        title = change.get("heading") or ""
        section = find_section(markdown, title)
        if section is None:
            raise ApplyError(f'section "{title}" not found in README')
        block = section_block(change["markdown"], lines[section.start])
        return splice(lines, section.start, section.end, block)

    anchor = change.get("after")
    if anchor:
        section = find_section(markdown, anchor)
        if section is None:
            raise ApplyError(f'anchor section "{anchor}" not found in README')
        insert_at = section.end
    else:
        insert_at = len(lines)
    block = section_block(change["markdown"], None)
    return splice(lines, insert_at, insert_at, block)


def merge_readme(original: str, changes: list[dict]) -> tuple[str, list[dict], list[tuple[dict, str]]]:
    merged = original
    applied: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for change in changes:
        try:
            merged = apply_change(merged, change)
        except ApplyError as exc:
            skipped.append((change, str(exc)))
        else:
            applied.append(change)
    return merged, applied, skipped


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------


def diff_text(report: Report, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    parts: list[str] = []
    for change in report.files:
        header = f"=== {change.status.upper()} {change.filename} (+{change.additions} -{change.deletions}) ==="
        parts.append(header)
        parts.append(change.patch or "(no patch: binary file, mode change, or diff too large)")
    text = "\n\n".join(parts) if parts else "(no file changes)"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... diff truncated at {max_chars} characters ..."
    return text


def commit_metadata(report: Report) -> str:
    lines = [
        f"- sha: {report.head.sha}",
        f"- author: {report.head.author}",
        f"- date: {report.head.date}",
        f"- message: {report.head.message.splitlines()[0] if report.head.message else '(empty)'}",
        f"- url: {report.head.url}",
    ]
    if len(report.commits) > 1:
        lines.append(f"- commits in range: {len(report.commits)}")
        for commit in report.commits[:-1]:
            subject = commit.message.splitlines()[0] if commit.message else ""
            lines.append(f"  - {commit.sha} {subject}")
    return "\n".join(lines)


def build_messages(report: Report, readme: str, repo_name: str, branch: str, max_diff_chars: int) -> list[dict]:
    user = f"""\
Repository: {repo_name}
Branch: {branch}

Commit under review:
{commit_metadata(report)}

Changed files:
{diff_text(report, max_diff_chars)}

Current README.md (source of truth, {len(readme.splitlines())} lines).
Existing sections you may target, by exact title:
{outline(readme) or "(none)"}

<<<README>>>
{readme}
<<<END README>>>

Task: given the code diff above, identify which documentation sections are
affected (README install/usage, API endpoints, changelog) and generate updated
markdown for those sections only. Merge the changes into the current README
rather than rewriting it from scratch: reuse its existing sections, wording and
structure, and return only the sections that must change. Respond with JSON
matching the schema in the system prompt."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------
# LLM call (plain HTTP, matching agent.py)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


class LLMError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def load_llm_config(args: argparse.Namespace) -> LLMConfig | None:
    api_key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = (
        args.base_url
        or os.environ.get("LLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = (
        args.model
        or os.environ.get("LLM_MODEL", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    return LLMConfig(api_key=api_key, base_url=base_url.rstrip("/"), model=model)


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in model output: {cleaned[:200]!r}")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    return data


def normalize_changes(data: dict) -> list[dict]:
    raw = data.get("changes")
    if raw is None:
        raise ValueError('model output is missing the "changes" field')
    if not isinstance(raw, list):
        raise ValueError('"changes" must be a list')
    changes: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each change must be an object")
        action = ACTION_ALIASES.get(str(item.get("action", "")).strip().lower())
        markdown = item.get("markdown")
        if not action:
            raise ValueError(f"unknown action: {item.get('action')!r}")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError('change is missing "markdown"')
        if action == "replace" and not item.get("heading"):
            raise ValueError('replace change is missing "heading"')
        changes.append(
            {
                "action": action,
                "heading": item.get("heading"),
                "after": item.get("after"),
                "markdown": markdown,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return changes


def error_message(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or response.reason or "").strip()[:300]
    if isinstance(body, dict):
        detail = body.get("error")
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        if detail:
            return str(detail)
        if body.get("message"):
            return str(body["message"])
    return str(body)[:300]


def post_chat(config: LLMConfig, payload: dict, attempts: int = 3) -> requests.Response:
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    url = f"{config.base_url}/chat/completions"
    response: requests.Response | None = None
    for attempt in range(attempts):
        response = requests.post(url, json=payload, headers=headers, timeout=180)
        if response.status_code in RETRY_STATUS and attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    assert response is not None
    return response


def call_llm(config: LLMConfig, messages: list[dict]) -> tuple[dict, int]:
    """POST /chat/completions and return (parsed JSON object, total tokens)."""
    payload = {
        "model": config.model,
        "temperature": 0,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    response = post_chat(config, payload)
    if response.status_code == 400 and "response_format" in payload:
        # Not every OpenAI-compatible endpoint supports JSON mode.
        payload.pop("response_format")
        response = post_chat(config, payload)

    if response.status_code == 401:
        raise LLMError("the LLM rejected the API key (401). Check LLM_API_KEY in .env", exit_code=2)
    if response.status_code == 429:
        raise LLMError("the LLM rate limit was hit; retry shortly or send a smaller diff")
    if not response.ok:
        raise LLMError(f"LLM error {response.status_code}: {error_message(response)}")

    try:
        body = response.json()
        text = body["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected LLM response: {(response.text or '')[:200]}")
    tokens = int((body.get("usage") or {}).get("total_tokens") or 0)
    try:
        return parse_json_object(text), tokens
    except ValueError as exc:
        raise LLMError(f"unusable model output: {exc}")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def print_diff(before: str, after: str, from_label: str, to_label: str) -> None:
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
            n=3,
        )
    )
    if not diff:
        print(paint("  (no textual changes)", "dim"))
        return
    for line in diff:
        if line.startswith(("---", "+++")):
            print(paint(line, "dim"))
        elif line.startswith("+"):
            print(paint(line, "green"))
        elif line.startswith("-"):
            print(paint(line, "red"))
        elif line.startswith("@@"):
            print(paint(line, "cyan"))
        else:
            print(line)


def describe_change(change: dict) -> str:
    if change["action"] == "replace":
        target = f"## {change['heading']}"
    else:
        target = f"new section after ## {change['after']}" if change.get("after") else "new section at end"
    reason = f" - {change['reason']}" if change["reason"] else ""
    return f"{target}{reason}"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False)
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="docs_gen.py",
        description="Update README sections affected by a commit diff, using an LLM.",
    )
    parser.add_argument("repo", nargs="?", help='repository as "owner/name" (or set GITHUB_REPO)')
    parser.add_argument("-b", "--branch", help="branch to inspect (default: repository default branch)")
    parser.add_argument("--since", metavar="SHA", help="diff from this commit (default: head's first parent)")
    parser.add_argument("--until", metavar="REF", help="diff up to this ref (default: branch head)")
    parser.add_argument(
        "--readme", default=DEFAULT_README, metavar="PATH", help=f"README to update (default {DEFAULT_README})"
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-o", "--out", metavar="PATH", help="write the merged README here")
    output.add_argument("--write", action="store_true", help="overwrite the README in place")
    parser.add_argument("--dry-run", action="store_true", help="print the prompt without calling the LLM")
    parser.add_argument("--model", metavar="NAME", help=f"model to use (default {DEFAULT_MODEL} or LLM_MODEL)")
    parser.add_argument("--base-url", metavar="URL", help="OpenAI-compatible endpoint (or LLM_BASE_URL)")
    parser.add_argument(
        "--max-diff-chars",
        type=int,
        default=DEFAULT_MAX_DIFF_CHARS,
        metavar="N",
        help=f"diff characters sent to the model (default {DEFAULT_MAX_DIFF_CHARS})",
    )
    return parser.parse_args(argv)


def resolve_repo(args: argparse.Namespace) -> str:
    return (args.repo or os.environ.get("GITHUB_REPO", "")).strip()


def load_readme(path: Path) -> tuple[str, str] | None:
    """Return (text with \\n newlines, the file's newline style), or None."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        error(f"README not found: {path} - pass --readme PATH")
        return None
    except OSError as exc:
        error(f"cannot read {path}: {exc}")
        return None
    try:
        text = raw.decode("utf-8-sig")  # strip a BOM, which would hide the first heading
    except UnicodeDecodeError:
        error(f"README is not valid UTF-8: {path}")
        return None
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def main(argv: list[str] | None = None) -> int:
    configure_output()
    load_dotenv(ENV_FILE)
    args = parse_args(argv)

    repo_name = resolve_repo(args)
    if not repo_name:
        error("no repository given. Pass one as an argument or set GITHUB_REPO in .env")
        return 2
    if args.max_diff_chars < 1:
        error("--max-diff-chars must be at least 1")
        return 2

    config = load_llm_config(args)
    if config is None and not args.dry_run:
        error("missing LLM API key. Set LLM_API_KEY (or OPENAI_API_KEY) in .env")
        return 2

    readme_path = Path(args.readme)
    loaded = load_readme(readme_path)
    if loaded is None:
        return 2
    readme, newline = loaded
    if len(readme) > 120_000:
        warn(f"README is {len(readme):,} characters; large READMEs may be truncated by the model")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        warn("GITHUB_TOKEN is missing; using anonymous access (60 requests/hour)")

    # retry=0: one-shot commands should report a rate limit instead of
    # sleeping through PyGithub's long exponential backoff.
    github = Github(auth=Auth.Token(token) if token else None, per_page=100, retry=0)
    try:
        repo = github.get_repo(repo_name)
        branch = args.branch or repo.default_branch
        head: Commit = repo.get_commit(sha=args.until or branch)
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

    messages = build_messages(report, readme, repo_name, branch, args.max_diff_chars)

    if args.dry_run:
        print(paint(f"dry run: prompt for {report.head.sha} ({len(messages[1]['content']):,} chars)", "bold"))
        print()
        for message in messages:
            print(paint(f"--- {message['role']} ---", "dim"))
            print(message["content"])
            print()
        return 0

    assert config is not None  # guarded above
    try:
        data, tokens = call_llm(config, messages)
    except LLMError as exc:
        error(str(exc))
        return exc.exit_code
    except requests.RequestException as exc:
        error(f"cannot reach the LLM endpoint: {exc}")
        return 1
    except ValueError as exc:
        error(f"unusable model output: {exc}")
        return 1

    try:
        changes = normalize_changes(data)
    except ValueError as exc:
        error(f"unusable model output: {exc}")
        return 1

    summary = str(data.get("summary") or "").strip()
    print()
    print(f"{stamp()} {paint(config.model, 'bold')}  {paint(f'{tokens:,} tokens', 'dim')}")
    if summary:
        print(f"  impact   {summary}")

    if not changes:
        print()
        print(paint("  no documentation changes needed for this commit", "dim"))
        return 0

    merged, applied, skipped = merge_readme(readme, changes)

    print()
    if applied:
        print(paint(f"  applied {len(applied)} change(s):", "bold"))
        for change in applied:
            print(f"    {paint(change['action'], 'green')}  {describe_change(change)}")
    for change, reason in skipped:
        print(paint(f"  skipped: {describe_change(change)} ({reason})", "yellow"))

    if not applied:
        error("nothing could be applied; README left untouched")
        return 1

    if merged == readme:
        print(paint("  merged README is identical to the current one; nothing to write", "dim"))
        return 0

    out_path = readme_path if args.write else Path(args.out) if args.out else readme_path.with_name(
        f"{readme_path.stem}.generated{readme_path.suffix}"
    )

    print()
    print_diff(readme, merged, f"current {readme_path.name}", f"updated {readme_path.name}")
    print()
    # Keep the README's own line endings so a Windows checkout does not turn
    # into a whole-file diff.
    write_text(out_path, merged if newline == "\n" else merged.replace("\n", newline))
    verb = "updated" if args.write else "wrote"
    print(f"{stamp()} {verb} {paint(str(out_path), 'bold')} ({len(merged.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())