# Watcher — AI-Powered Documentation Agent

Watcher is an AI-powered developer tool that monitors a GitHub repository, analyzes code changes, and proposes targeted README updates using a large language model (LLM). It can prepare documentation changes for review and supports publishing through a dedicated branch and pull request workflow.

The project also includes a **Documentation Command Center**, a web dashboard for configuring and running documentation workflows.

## Table of Contents

- [Problem Statement](#problem-statement)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Environment Configuration](#environment-configuration)
- [Usage](#usage)
- [Documentation Command Center](#documentation-command-center)
- [Example Workflow](#example-workflow)
- [Safety and Review](#safety-and-review)
- [Limitations](#limitations)
- [Future Improvements](#future-improvements)
- [Author](#author)

## Problem Statement

Documentation often becomes outdated when developers add features, modify functions, introduce endpoints, or change application configuration.

Maintaining documentation manually takes time and is easy to forget. Rewriting an entire README for a small code change can also introduce unnecessary modifications.

Watcher aims to automate this process by analyzing repository changes and using an LLM to identify documentation that may need updating while preserving unaffected content.

## Key Features

- **GitHub repository monitoring:** Checks a configured repository for new commits.
- **Commit and diff analysis:** Retrieves relevant changes to understand what changed in the codebase.
- **AI-powered documentation:** Uses an LLM to propose documentation updates based on code changes.
- **Targeted README updates:** Focuses on relevant documentation sections rather than unnecessarily rewriting the entire file.
- **Automated publishing workflow:** Supports creating a documentation branch, committing changes, and opening a pull request when publishing is enabled.
- **Preview mode:** Generates a proposed update without publishing changes.
- **One-time processing:** Processes the current repository head and exits.
- **Continuous monitoring:** Supports repeated repository checks through the watch command.
- **Duplicate-change avoidance:** Checks whether a meaningful documentation update is needed before publishing.
- **Configurable LLM provider:** Supports an OpenAI-compatible API endpoint, including the Gemini OpenAI-compatible endpoint.
- **Documentation Command Center:** Provides a browser-based interface for configuring and running workflows.
- **Reviewable output:** Produces a proposed documentation update and pull request description for review.

## How It Works

1. Connects to the configured GitHub repository.
2. Identifies the latest commit that needs processing.
3. Retrieves the relevant commit information and code diff.
4. Sends the current README and relevant changes to the configured LLM.
5. Generates proposed documentation changes based on the code changes.
6. Checks whether the proposed documentation contains meaningful changes.
7. Displays the proposed update in preview mode or prepares it for publishing.
8. When publishing is enabled, creates the documentation branch and pull request workflow.

## Architecture

```text
GitHub Repository
       |
       v
Repository Monitoring
       |
       v
Commit and Diff Analysis
       |
       v
Documentation Generator
       |
       v
Configured LLM API
       |
       v
Proposed README Update
       |
       v
Change Review
       |
       +------ Preview Mode
       |
       +------ Publishing Workflow
                    |
                    v
             Documentation Branch
                    |
                    v
              Pull Request
```

## Tech Stack

- **Language:** Python 3.13
- **GitHub integration:** PyGithub and GitHub APIs
- **HTTP requests:** Requests
- **Configuration:** python-dotenv
- **AI integration:** OpenAI-compatible LLM API
- **Web interface:** HTML, CSS, and JavaScript
- **Backend:** Python dashboard server

## Project Structure

```text
Watcher/
├── main.py
├── watcher.py
├── docs_gen.py
├── publisher.py
├── dashboard_server.py
├── dashboard/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

The exact structure may vary slightly with your current branch. Keep the file tree aligned with the files actually committed to the repository.

## Prerequisites

Before running Watcher, make sure you have:

- Python 3.10 or newer; Python 3.13 is the development version used for this project.
- Git installed on your machine.
- A GitHub repository you are authorized to access.
- A GitHub token with the permissions needed for your intended operations.
- An API key for a supported LLM provider.
- Internet connectivity for GitHub and LLM API requests.

## Installation

### 1. Clone the repository

Replace the placeholders with your actual GitHub repository details.

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution, follow your machine's Python virtual-environment activation guidance rather than changing system-wide security settings unnecessarily.

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Create your local environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your own credentials and configuration.

## Environment Configuration

Example configuration for Gemini through its OpenAI-compatible endpoint:

```dotenv
GITHUB_TOKEN=your_github_token
GITHUB_REPO=YOUR_USERNAME/YOUR_REPOSITORY

LLM_API_KEY=your_gemini_api_key
LLM_MODEL=gemini-2.5-flash
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/

POLL_INTERVAL=30
```

Configuration notes:

- `GITHUB_TOKEN` authenticates GitHub operations.
- `GITHUB_REPO` identifies the repository in `owner/repository` format.
- `LLM_API_KEY` contains your provider's API key.
- `LLM_MODEL` identifies a model supported by your provider and account.
- `LLM_BASE_URL` specifies the OpenAI-compatible API endpoint.
- `POLL_INTERVAL` configures the intended polling interval where supported by the running workflow.

Use a provider and model that are enabled for your account. The example above reflects the Gemini configuration used successfully during development; availability and usage limits depend on the provider.

**Security:** Never commit `.env`, API keys, or access tokens. Keep `.env` ignored by Git and share only the placeholder-based `.env.example`.

For publishing, ensure the GitHub token has sufficient permissions to read repository changes, create branches and commits, and open pull requests.

## Usage

Run commands from the Watcher project directory with the virtual environment activated.

### Process the latest commit once

```powershell
python main.py once
```

This processes the current repository head according to the configured repository and saved processing state.

### Preview a documentation update

```powershell
python main.py once --dry-run
```

This is the recommended command for safely demonstrating Watcher. It generates and displays a proposed documentation update without publishing changes.

### Run continuous monitoring

```powershell
python main.py watch
```

The watch command runs the continuous monitoring workflow. Stop it using `Ctrl+C`.

### Inspect available command options

```powershell
python main.py --help
python main.py once --help
python main.py watch --help
```

Use the options shown by your installed version. For example, the one-time command supports options such as `--readme`, `--path`, `--base`, `--model`, `--base-url`, and `--dry-run`.

### Select a repository explicitly

The one-time command accepts a repository argument:

```powershell
python main.py once YOUR_USERNAME/YOUR_REPOSITORY --dry-run
```

You can also configure `GITHUB_REPO` in `.env` instead.

## Documentation Command Center

Watcher includes a web dashboard called **Documentation Command Center**.

The dashboard lets you configure and execute documentation workflows through a browser instead of relying exclusively on terminal commands.

### Start the dashboard

From the Watcher project folder:

```powershell
python dashboard_server.py
```

Open the dashboard at:

http://127.0.0.1:8020

### Dashboard capabilities

- View backend connection status.
- Configure the target GitHub repository.
- Specify the README path.
- Configure the polling interval.
- Enable preview mode to avoid publishing changes.
- Process the latest commit once.
- Start continuous monitoring.
- View workflow status and exit codes.
- Inspect run output.

The dashboard uses the Python backend to execute the documentation workflow. Keep API credentials on the backend; they should never be embedded in frontend JavaScript.

### Verified development workflow

During development, Watcher successfully processed a commit from `Roopesh431/BugHunter` using `gemini-2.5-flash` in preview mode.

The run generated a proposed README update and a pull request description, and completed with exit code `0`.

This demonstrates the preview workflow. A successful preview does not mean a branch or pull request was published.

## Example Workflow

A typical run may:

1. Detect a new commit in a GitHub repository.
2. Identify changed files and relevant code changes.
3. Determine which documentation sections may be affected.
4. Generate an updated README proposal.
5. Show a summary of the proposed changes.
6. Prepare a documentation branch and pull request when publishing is enabled.

Example output below is illustrative; exact messages depend on the repository and the changes being processed.

```text
Checking repository
Reading README
Analyzing commit changes
Generating documentation update
Preparing proposed README changes
Preview completed
```

## Safety and Review

- Preview changes before publishing.
- Review generated documentation for correctness.
- Preserve content that is not supported by the code changes.
- Keep API credentials out of the repository.
- Do not merge generated documentation automatically without appropriate review.
- Confirm the target repository and branch before enabling publishing.
- Check the generated diff before merging a pull request.

## Limitations

- Documentation quality depends on the code diff, existing README structure, and LLM output.
- Large or incomplete diffs may limit the model's understanding of the change.
- Provider rate limits, network errors, and API outages can interrupt processing.
- LLM-generated documentation can contain inaccuracies and requires review.
- The current workflow primarily targets README documentation.
- Repository permissions may prevent branch creation or pull request publishing.
- Continuous monitoring requires the process to remain running.

## Future Improvements

- Support multiple documentation files.
- Add automated documentation quality checks.
- Integrate with CI/CD pipelines.
- Improve run history and error reporting in the dashboard.
- Add documentation freshness and coverage metrics.
- Support configurable publishing policies and approval workflows.

## Author

**Roopesh Lingam**

Built as a hackathon project exploring AI-assisted developer tooling and documentation automation.
