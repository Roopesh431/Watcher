const $ = id => document.getElementById(id);

let toastTimer;

function toast(message) {
  const element = $("toast");

  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);

  toastTimer = setTimeout(() => {
    element.classList.remove("visible");
  }, 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.detail || "Request failed");
  }

  return data;
}

function payload() {
  return {
    repo: $("repo").value.trim() || null,
    readme: $("readme").value.trim() || "Readme.md",
    interval: Number($("interval").value || 30),
    dry_run: $("dry-run").checked
  };
}

async function refresh() {
  try {
    const data = await api("/api/status");
    const run = data.run;

    $("connection").innerHTML =
      '<span class="connection-dot"></span>Backend connected';

    $("connection").classList.remove("disconnected");
    $("connection").classList.add("connected");

    $("repo").value = $("repo").value || data.default_repo || "";
    $("repo-metric").textContent = $("repo").value || "Not set";

    $("run-status").textContent = run.running
      ? "Running…"
      : (run.kind ? "Finished" : "Idle");

    $("exit-code").textContent = run.exit_code ?? "—";

    $("logs").textContent =
      run.error || run.output || "No run output yet.";

    const indicator = $("run-indicator");

    indicator.classList.remove("running", "finished", "error");

    if (run.running) {
      indicator.classList.add("running");
    } else if (run.error || (run.exit_code !== null &&
                            run.exit_code !== undefined &&
                            run.exit_code !== 0)) {
      indicator.classList.add("error");
    } else if (run.kind) {
      indicator.classList.add("finished");
    }

    $("once").disabled = run.running;
    $("watch").disabled = run.running;
  } catch (error) {
    $("connection").innerHTML =
      '<span class="connection-dot"></span>Backend unavailable';

    $("connection").classList.remove("connected");
    $("connection").classList.add("disconnected");

    $("logs").textContent = error.message;

    $("run-indicator").classList.remove("running", "finished");
    $("run-indicator").classList.add("error");
  }
}

async function run(path) {
  try {
    const data = await api(path, {
      method: "POST",
      body: JSON.stringify(payload())
    });

    toast(data.message);
    refresh();
  } catch (error) {
    toast(error.message);
  }
}

$("once").onclick = () => run("/api/run/once");
$("watch").onclick = () => run("/api/run/watch");
$("refresh").onclick = refresh;

refresh();
setInterval(refresh, 2500);