const page = document.querySelector("[data-client-page]");
const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
const feedback = page?.querySelector("[data-client-feedback]");
const base = "/api/v1/clients/" + page?.dataset.clientId;

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRFToken": token, ...(options.headers || {}) },
    credentials: "same-origin",
  });
  const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || "Não foi possível salvar.");
  return body;
}

page?.querySelector("[data-owner-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(base + "/owner", {
      method: "PATCH",
      body: JSON.stringify({ owner_id: event.currentTarget.elements.owner_id.value }),
    });
    location.reload();
  } catch (error) { feedback.textContent = error.message; }
});

page?.querySelector("[data-contact-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(base + "/contacts", {
      method: "POST",
      body: JSON.stringify({
        name: form.elements.name.value.trim(),
        email: form.elements.email.value.trim(),
        job_title: form.elements.job_title.value.trim(),
        phone: form.elements.phone.value.trim(),
        is_primary: form.elements.is_primary.checked,
      }),
    });
    location.reload();
  } catch (error) { feedback.textContent = error.message; }
});

page?.querySelectorAll("[data-contact-deactivate]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await api(base + "/contacts/" + button.dataset.contactDeactivate, { method: "DELETE" });
      location.reload();
    } catch (error) { feedback.textContent = error.message; }
  });
});
