
const $ = id => document.getElementById(id);

function toast(message) {
  $("toast").textContent = message;
  $("toast").style.display = "block";
  setTimeout(() => $("toast").style.display = "none", 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Request failed");
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

    $("connection").textContent = "Backend connected";
    $("repo").value = $("repo").value || data.default_repo || "";
    $("repo-metric").textContent = $("repo").value || "Not set";
    $("run-status").textContent = run.running
      ? "Running…" : (run.kind ? "Finished" : "Idle");
    $("exit-code").textContent = run.exit_code ?? "—";
    $("logs").textContent = run.error || run.output || "No run output yet.";

    $("once").disabled = run.running;
    $("watch").disabled = run.running;
  } catch (error) {
    $("connection").textContent = "Backend unavailable";
    $("logs").textContent = error.message;
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
