const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
    headers.set("X-CSRFToken", csrfToken);
  }
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({ error: "Resposta inválida." }));
  if (!response.ok) {
    const error = new Error(payload.error || "Não foi possível concluir.");
    error.code = payload.code;
    throw error;
  }
  return payload;
}

export const loadBriefing = () => request("/api/v1/dashboard/briefing");
export const loadDailyBriefing = () => request("/api/v1/dashboard/daily-briefing");
export const loadTimeline = () => request("/api/v1/dashboard/timeline?limit=8");
export const loadFocus = () => request("/api/v1/dashboard/focus?limit=8");
export const markBriefingSeen = () => request("/api/v1/dashboard/seen", { method: "POST" });

export const sendKanbanRequest = (payload) => request("/api/v1/kanban/requests", {
  method: "POST",
  body: JSON.stringify(payload),
});

export const startAction = (action) => request(`/api/v1/actions/${action.id}`, {
  method: "PATCH",
  body: JSON.stringify({ status: "in_progress", expected_version: action.version }),
});
