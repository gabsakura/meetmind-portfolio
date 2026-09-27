const page = document.querySelector("[data-team-page]");
const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
const feedback = page?.querySelector("[data-team-feedback]");

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRFToken": token, ...(options.headers || {}) },
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || "Não foi possível salvar.");
  return body;
}

page?.querySelector("[data-team-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/v1/admin/teams", {
      method: "POST",
      body: JSON.stringify({ name: form.elements.name.value.trim(), manager_id: form.elements.manager_id.value || null }),
    });
    location.reload();
  } catch (error) { feedback.textContent = error.message; }
});

page?.querySelectorAll("[data-user-form]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/v1/admin/users/" + form.dataset.userId, {
        method: "PATCH",
        body: JSON.stringify({
          role: form.elements.role.value,
          team_id: form.elements.team_id.value || null,
          active: form.elements.active.checked,
        }),
      });
      feedback.textContent = "Usuário atualizado.";
    } catch (error) { feedback.textContent = error.message; }
  });
});
