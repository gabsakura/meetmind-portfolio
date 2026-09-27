const page = document.querySelector("[data-routing-page]");
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || "Não foi possível concluir a operação.");
    error.code = body.code;
    throw error;
  }
  return body;
}

function selectedValues(select) {
  return [...select.selectedOptions].map((option) => option.value);
}

function renderDraft(workspace, draft) {
  let editor = workspace.querySelector("[data-draft-editor]");
  if (editor.hidden) {
    editor.append(document.querySelector("#draft-editor-template").content.cloneNode(true));
    editor.hidden = false;
  }
  workspace.draft = draft;
  editor.querySelector("[data-draft-status]").textContent = `Estado: ${draft.status}`;
  editor.querySelector("[data-draft-revision]").textContent = `Revisão ${draft.current_revision}`;
  editor.querySelector("[data-draft-subject]").value = draft.subject;
  editor.querySelector("[data-draft-body]").value = draft.body;
  const approved = draft.status === "approved";
  editor.querySelector("[data-save-draft]").disabled = !["draft", "reviewed"].includes(draft.status);
  editor.querySelector("[data-approve-draft]").disabled = !["draft", "reviewed"].includes(draft.status);
  editor.querySelector("[data-copy-draft]").disabled = !approved;
  editor.querySelector("[data-confirm-draft]").disabled = !approved || draft.channel === "calendar";
  editor.querySelector("[data-calendar-form]").hidden = !(approved && draft.channel === "calendar");
}

async function markCustomerReturn(workspace) {
  const card = workspace.closest("[data-routing-card]");
  await api(`/api/v1/kanban/requests/${workspace.dataset.requestId}/customer-return`, {
    method: "POST",
    body: JSON.stringify({ version: Number(card.dataset.version), returned: true }),
  });
}

page?.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-draft-form]");
  if (!form) return;
  event.preventDefault();
  const workspace = form.closest("[data-communication]");
  const feedback = workspace.querySelector("[data-communication-feedback]");
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  feedback.textContent = "Criando rascunho…";
  try {
    const draft = await api("/api/v1/communication-drafts", {
      method: "POST",
      body: JSON.stringify({
        client_id: workspace.dataset.clientId,
        meeting_id: workspace.dataset.meetingId || null,
        action_id: workspace.dataset.actionId || null,
        request_id: workspace.dataset.requestId,
        draft_type: form.elements.draft_type.value,
        channel: form.elements.channel.value,
        recipient_contact_ids: selectedValues(form.elements.recipient_contact_ids),
        generate_with_ai: form.elements.generate_with_ai.checked,
        subject: form.elements.subject.value,
        body: form.elements.body.value,
      }),
    });
    form.hidden = true;
    renderDraft(workspace, draft);
    feedback.textContent = "Rascunho criado. Revise antes de aprovar.";
  } catch (error) {
    feedback.textContent = error.message;
    button.disabled = false;
  }
});

page?.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-save-draft], [data-approve-draft], [data-copy-draft], [data-confirm-draft]");
  if (!action) return;
  const workspace = action.closest("[data-communication]");
  const editor = workspace.querySelector("[data-draft-editor]");
  const feedback = workspace.querySelector("[data-communication-feedback]");
  const draft = workspace.draft;
  action.disabled = true;
  try {
    if (action.matches("[data-save-draft]")) {
      renderDraft(workspace, await api(`/api/v1/communication-drafts/${draft.id}`, { method: "PATCH", body: JSON.stringify({ version: draft.version, subject: editor.querySelector("[data-draft-subject]").value, body: editor.querySelector("[data-draft-body]").value, recipient_contact_ids: draft.recipients.map((item) => item.id) }) }));
      feedback.textContent = "Nova revisão salva.";
    } else if (action.matches("[data-approve-draft]")) {
      renderDraft(workspace, await api(`/api/v1/communication-drafts/${draft.id}/approve`, { method: "POST", body: JSON.stringify({ version: draft.version }) }));
      feedback.textContent = "Texto aprovado. Agora copie ou agende.";
    } else if (action.matches("[data-copy-draft]")) {
      await navigator.clipboard.writeText(`${draft.subject}\n\n${draft.body}`);
      feedback.textContent = "Texto copiado. Confirme o envio após usar o canal escolhido.";
      action.disabled = false;
    } else {
      renderDraft(workspace, await api(`/api/v1/communication-drafts/${draft.id}/confirm-send`, { method: "POST", body: JSON.stringify({ version: draft.version, channel: draft.channel }) }));
      await markCustomerReturn(workspace);
      feedback.textContent = "Envio e retorno ao cliente registrados no histórico.";
      window.location.reload();
    }
  } catch (error) {
    feedback.textContent = error.message;
    action.disabled = false;
  }
});

page?.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-calendar-form]");
  if (!form) return;
  event.preventDefault();
  const workspace = form.closest("[data-communication]");
  const feedback = workspace.querySelector("[data-communication-feedback]");
  try {
    const draft = await api(`/api/v1/communication-drafts/${workspace.draft.id}/schedule`, { method: "POST", body: JSON.stringify({ version: workspace.draft.version, provider: form.elements.provider.value, starts_at: form.elements.starts_at.value, duration_minutes: Number(form.elements.duration_minutes.value) }) });
    renderDraft(workspace, draft);
    await markCustomerReturn(workspace);
    feedback.textContent = "Evento, convite e retorno ao cliente registrados.";
    window.location.reload();
  } catch (error) { feedback.textContent = error.message; }
});

page?.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-no-return-form]");
  if (!form) return;
  event.preventDefault();
  const workspace = form.closest("[data-communication]");
  const card = workspace.closest("[data-routing-card]");
  try {
    await api(`/api/v1/kanban/requests/${workspace.dataset.requestId}/customer-return`, { method: "POST", body: JSON.stringify({ version: Number(card.dataset.version), returned: false, not_required_reason: form.elements.reason.value }) });
    window.location.reload();
  } catch (error) { workspace.querySelector("[data-communication-feedback]").textContent = error.message; }
});

async function updateRequest(id, payload) {
  const response = await fetch(`/api/v1/kanban/requests/${id}`, {
    method: "PATCH",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || "Não foi possível atualizar o encaminhamento.");
    error.code = body.code;
    throw error;
  }
  return body;
}

page?.querySelector("[data-new-routing-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const feedback = page.querySelector("[data-routing-feedback]");
  button.disabled = true;
  button.textContent = "Enviando";
  feedback.textContent = "";
  try {
    const response = await fetch("/api/v1/kanban/requests", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({
        client_id: form.elements.client_id.value,
        request_type: form.elements.request_type.value,
        source_channel: form.elements.source_channel.value,
        note: form.elements.note.value.trim(),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || "Não foi possível registrar o pedido.");
    feedback.textContent = `Solicitação enviada para ${body.department}.`;
    window.location.reload();
  } catch (error) {
    feedback.textContent = error.message;
    button.disabled = false;
    button.textContent = "Enviar para o setor";
  }
});
page?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-routing-message]");
  if (!button) return;
  const message = button.closest(".routing-message")?.querySelector("pre")?.textContent || "";
  try {
    await navigator.clipboard.writeText(message);
    button.textContent = "Copiada";
  } catch {
    page.querySelector("[data-routing-feedback]").textContent = "Não foi possível copiar a mensagem.";
  }
});
page?.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-routing-form]");
  if (!form) return;
  event.preventDefault();
  const card = form.closest("[data-routing-card]");
  const button = form.querySelector('button[type="submit"]');
  const feedback = page.querySelector("[data-routing-feedback]");
  button.disabled = true;
  button.textContent = "Salvando";
  feedback.textContent = "";
  try {
    await updateRequest(card.dataset.id, {
      expected_version: Number(card.dataset.version),
      assigned_to_id: form.elements.assigned_to_id.value || null,
      due_at: form.elements.due_at.value || null,
      status: form.elements.status.value,
      response: form.elements.response.value.trim() || null,
    });
    feedback.textContent = "Encaminhamento atualizado.";
    window.location.reload();
  } catch (error) {
    feedback.textContent = error.code === "version_conflict"
      ? "Outra pessoa atualizou este encaminhamento. Recarregue a página."
      : error.message;
    button.disabled = false;
    button.textContent = "Salvar";
  }
});
