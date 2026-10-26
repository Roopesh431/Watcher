# Watcher — AI-Powered Documentation Agent

Automatically keep project documentation in sync with code changes. Watcher monitors a GitHub repository, analyzes new commits, uses an LLM to update only the affected documentation sections, and opens a pull request for review.

## Problem

Documentation often becomes outdated as developers modify code, add endpoints, or change configuration. Updating documentation manually takes time and is easy to forget.

Watcher automates this workflow while preserving existing README content that does not need to change.

## Key Features

- **GitHub monitoring:** Polls a repository for new commits at a configurable interval.
- **Commit diff analysis:** Identifies changed files, additions, deletions, and patch content.
- **AI-powered documentation:** Uses an LLM to determine which README sections need updating.
- **Targeted README updates:** Preserves unaffected sections instead of rewriting the entire document.
- **Automated pull requests:** Creates a documentation branch, commits meaningful changes, and opens a PR for review.
- **Duplicate prevention:** Avoids creating unnecessary commits or pull requests when documentation has not meaningfully changed.
- **Resilience:** Handles common API failures, rate limits, and retryable errors.
- **Safe preview:** Supports generating a proposed README without immediately overwriting the existing file.

## Workflow

1. Monitor the selected GitHub repository.
2. Detect a new commit and retrieve its diff.
3. Send the relevant code changes and current README to the LLM.
4. Identify affected sections such as installation, usage, API reference, configuration, and changelog.
5. Merge only the proposed section changes into the existing README.
6. Check whether the documentation has meaningfully changed.
7. Publish the updated documentation on a dedicated branch and open a pull request.

## Tech Stack

- Python 3.13
- PyGithub
- Requests
- python-dotenv
- OpenAI-compatible LLM API
- GitHub REST API

## Project Structure

```text
watcher/
├── watcher.py
├── docs_gen.py
├── publisher.py
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. Create and activate a virtual environment

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example file:

```powershell
copy .env.example .env
```

Configure your credentials in `.env`:

```dotenv
GITHUB_TOKEN=your_github_token
GITHUB_REPO=YOUR_USERNAME/YOUR_REPOSITORY

LLM_API_KEY=your_llm_api_key
LLM_MODEL=your_supported_model
LLM_BASE_URL=https://api.openai.com/v1

POLL_INTERVAL=30
```

Use an LLM provider and model supported by your configured API endpoint. Keep `.env` out of version control.

The GitHub token needs the repository permissions required to read commits and create branches, commits, and pull requests.

## Usage

### Watch continuously

```bash
python main.py watch
```

### Watch with a shorter polling interval

```bash
python main.py watch YOUR_USERNAME/YOUR_REPOSITORY --interval 15
```

### Process the current latest commit once

```bash
python main.py once
```

### Preview without publishing changes

```bash
python main.py watch --dry-run
```

### Run individual stages

Inspect a repository's latest commit:

```bash
python watcher.py YOUR_USERNAME/YOUR_REPOSITORY --once
```

Generate a proposed README:

```bash
python docs_gen.py YOUR_USERNAME/YOUR_REPOSITORY
```

Review the generated documentation before applying it. Use the generator's `--write` option only when you intend to update the local README.

## Example Output

After a code change is pushed, the workflow should produce output similar to:

```text
New commit detected
Changed files: 2
Documentation impact: API usage and configuration
README sections updated
Documentation branch created
Pull request opened
```

This is illustrative output; actual messages depend on the run.

## Safety and Review

- The existing README remains the source of truth.
- Only sections supported by the code diff should change.
- Unaffected sections are preserved.
- Meaningful documentation changes are proposed through a pull request rather than silently merged into the default branch.
- Review AI-generated documentation for correctness before merging.

## Limitations

- Documentation quality depends on the available code diff, the README structure, and the configured LLM.
- Binary files and incomplete GitHub diffs can limit what the agent understands.
- API rate limits and LLM availability can delay processing.
- The current publishing workflow targets the README; other documentation files may require additional integration.

## Future Improvements

- Support multiple documentation files.
- Add automated documentation quality checks.
- Integrate with CI/CD workflows.
- Add a web dashboard for monitoring runs and pull requests.
- Track documentation coverage and stale sections.

## Author

**Roopesh Lingam**

Built as a hackathon project exploring AI-assisted developer tooling and documentation automation.
