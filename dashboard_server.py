
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

UI = ROOT / "dashboard"
PORT = int(os.getenv("DASHBOARD_PORT", "8020"))
DEFAULT_REPO = os.getenv("GITHUB_REPO", "").strip()

app = FastAPI(title="Watcher Dashboard")
app.mount("/static", StaticFiles(directory=str(UI)), name="static")

lock = threading.Lock()

state = {
    "running": False,
    "kind": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "output": "",
    "error": None,
}


class RunRequest(BaseModel):
    repo: str | None = None
    readme: str = "Readme.md"
    dry_run: bool = True
    interval: int = 30


def validate_repo(value):
    repo = (value or DEFAULT_REPO).strip()
    parts = repo.split("/")

    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            status_code=400,
            detail="Enter a GitHub repository as owner/name.",
        )

    if any(not part.strip() for part in parts):
        raise HTTPException(status_code=400, detail="Invalid repository.")

    return repo


def execute(command, kind, timeout=900):
    with lock:
        state.update(
            running=True,
            kind=kind,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=None,
            exit_code=None,
            output="",
            error=None,
        )

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        output = result.stdout or ""

        if result.stderr:
            output += "\n" + result.stderr

        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=result.returncode,
                output=output[-18000:],
            )

    except subprocess.TimeoutExpired:
        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=124,
                error="Command timed out.",
            )

    except Exception as exc:
        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=1,
                error=str(exc),
            )


def start_job(command, kind):
    with lock:
        if state["running"]:
            raise HTTPException(
                status_code=409,
                detail="A Watcher job is already running.",
            )

        # Mark it running before launching the thread to avoid
        # two requests starting jobs at the same time.
        state.update(
            running=True,
            kind=kind,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=None,
            exit_code=None,
            output="",
            error=None,
        )

    def worker():
        # execute() also updates state when the process finishes.
        execute(command, kind)

    threading.Thread(target=worker, daemon=True).start()

    return {"ok": True, "message": f"{kind} started"}


@app.get("/")
def index():
    return FileResponse(UI / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    with lock:
        current = dict(state)

    return {
        "run": current,
        "default_repo": DEFAULT_REPO,
    }


@app.post("/api/run/once")
def run_once(request: RunRequest):
    repo = validate_repo(request.repo)

    command = [
        sys.executable,
        "main.py",
        "once",
        repo,
        "--readme",
        request.readme,
    ]

    if request.dry_run:
        command.append("--dry-run")

    return start_job(command, "Process latest commit")


def watch_worker(command):
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        lines = []

        for line in process.stdout:
            lines.append(line)

            with lock:
                state["output"] = "".join(lines)[-18000:]

        code = process.wait()

        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=code,
            )

    except Exception as exc:
        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=1,
                error=str(exc),
            )


@app.post("/api/run/watch")
def run_watch(request: RunRequest):
    repo = validate_repo(request.repo)

    if not 10 <= request.interval <= 3600:
        raise HTTPException(
            status_code=400,
            detail="Polling interval must be between 10 and 3600 seconds.",
        )

    command = [
        sys.executable,
        "main.py",
        "watch",
        repo,
        "--interval",
        str(request.interval),
        "--readme",
        request.readme,
    ]

    if request.dry_run:
        command.append("--dry-run")

    with lock:
        if state["running"]:
            raise HTTPException(
                status_code=409,
                detail="A Watcher job is already running.",
            )

        state.update(
            running=True,
            kind="Continuous watch",
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=None,
            exit_code=None,
            output="",
            error=None,
        )

    threading.Thread(
        target=watch_worker,
        args=(command,),
        daemon=True,
    ).start()

    return {"ok": True, "message": "Continuous watch started"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
