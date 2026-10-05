const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, ...(options.headers || {}) },
  });
  const body = response.status === 204 ? {} : await response.json();
  if (!response.ok) {
    const error = new Error(body.error || "Não foi possível concluir a operação.");
    error.code = body.code;
    throw error;
  }
  return body;
}

const modeConfig = {
  details: { title: "Detalhes da ação", submit: null, fields: [] },
  edit: { title: "Editar ação", submit: "Salvar alterações", fields: ["title", "owner", "due"] },
  accept: { title: "Confirmar e atribuir", submit: "Confirmar ação", fields: ["owner", "due"] },
  dismiss: { title: "Descartar rascunho", submit: "Confirmar descarte", fields: ["note"], note: "Justificativa obrigatória" },
  start: { title: "Iniciar ação", submit: "Confirmar início", fields: [] },
  complete: { title: "Concluir ação", submit: "Registrar conclusão", fields: ["note", "customer-notified", "follow-up"], note: "Resultado alcançado" },
  comment: { title: "Adicionar comentário", submit: "Salvar comentário", fields: ["note"], note: "Comentário" },
};

const page = document.querySelector("[data-actions-page]");
if (page) {
  const deliveryPanel = page.querySelector("[data-delivery-panel]");
  const deliveryForm = deliveryPanel?.querySelector("[data-delivery-form]");
  const deliveryFeedback = deliveryPanel?.querySelector("[data-delivery-feedback]");

  deliveryPanel?.querySelectorAll("[data-delivery-due]").forEach((item) => {
    const value = new Date(item.dateTime);
    if (!Number.isNaN(value.getTime())) item.textContent = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(value);
  });

  const setDeliveryFeedback = (message, tone = "") => {
    if (!deliveryFeedback) return;
    deliveryFeedback.textContent = message;
    deliveryFeedback.dataset.tone = tone;
  };
  const setDeliveryPending = (button, pending, label = "Enviando…") => {
    if (!button) return;
    if (pending) {
      button.dataset.originalLabel = button.textContent;
      button.textContent = label;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.originalLabel || button.textContent;
      button.disabled = false;
    }
  };
  const syncDeliveryProviders = () => {
    if (!deliveryForm) return;
    const assignee = deliveryForm.elements.assignee_id.selectedOptions[0];
    const providerSelect = deliveryForm.elements.calendar_provider;
    let providers = [];
    try { providers = JSON.parse(assignee?.dataset.providers || "[]"); } catch (_) { providers = []; }
    providerSelect.replaceChildren(new Option(providers.length ? "Selecione" : "Sem calendário conectado", ""));
    providers.forEach((provider) => providerSelect.add(new Option(provider.label, provider.id)));
    if (providers.length === 1) providerSelect.value = providers[0].id;
    providerSelect.disabled = providers.length === 0;
  };

  deliveryForm?.elements.assignee_id?.addEventListener("change", syncDeliveryProviders);
  deliveryForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = deliveryForm.querySelector('button[type="submit"]');
    const dueAt = new Date(deliveryForm.elements.due_at.value);
    if (Number.isNaN(dueAt.getTime())) return setDeliveryFeedback("Informe uma data e hora válidas.", "error");
    setDeliveryPending(button, true);
    setDeliveryFeedback("");
    try {
      const result = await api("/api/v1/team-deliveries", {
        method: "POST",
        body: JSON.stringify({
          assignee_id: deliveryForm.elements.assignee_id.value,
          title: deliveryForm.elements.title.value.trim(),
          description: deliveryForm.elements.description.value.trim(),
          due_at: dueAt.toISOString(),
          priority: deliveryForm.elements.priority.value,
          calendar_provider: deliveryForm.elements.calendar_provider.value,
        }),
      });
      sessionStorage.setItem("meetmind-delivery-feedback", result.calendar_status === "synced"
        ? `Entrega atribuída e adicionada ao calendário de ${result.assignee}.`
        : `Entrega atribuída a ${result.assignee}. A sincronização com o calendário ficou pendente.`);
      window.location.reload();
    } catch (error) {
      setDeliveryFeedback(error.message, "error");
      setDeliveryPending(button, false);
    }
  });

  const persistedDeliveryFeedback = sessionStorage.getItem("meetmind-delivery-feedback");
  if (persistedDeliveryFeedback) {
    setDeliveryFeedback(persistedDeliveryFeedback, "success");
    sessionStorage.removeItem("meetmind-delivery-feedback");
  }
  deliveryPanel?.addEventListener("click", async (event) => {
    const retry = event.target.closest("[data-delivery-retry]");
    const statusButton = event.target.closest("[data-delivery-status]");
    const trigger = retry || statusButton;
    if (!trigger) return;
    const card = trigger.closest("[data-delivery-card]");
    setDeliveryPending(trigger, true, retry ? "Sincronizando…" : "Salvando…");
    setDeliveryFeedback("");
    try {
      const result = retry
        ? await api(`/api/v1/team-deliveries/${card.dataset.deliveryId}/calendar/retry`, { method: "POST", body: "{}" })
        : await api(`/api/v1/team-deliveries/${card.dataset.deliveryId}`, {
          method: "PATCH",
          body: JSON.stringify({ status: statusButton.dataset.deliveryStatus, expected_version: Number(card.dataset.version) }),
        });
      sessionStorage.setItem("meetmind-delivery-feedback", retry && result.calendar_status !== "synced"
        ? "A entrega continua salva, mas o calendário ainda não respondeu. Verifique a conexão no Perfil."
        : retry ? "Calendário sincronizado." : "Entrega atualizada.");
      window.location.reload();
    } catch (error) {
      setDeliveryFeedback(error.code === "version_conflict" ? "Outra pessoa atualizou esta entrega. Recarregue a página." : error.message, "error");
      setDeliveryPending(trigger, false);
    }
  });

  const dialog = page.querySelector("[data-action-dialog]");
  const form = dialog?.querySelector("[data-action-form]");
  const pageFeedback = page.querySelector("[data-action-feedback]");
  let selectedCard = null;

  const syncFollowUp = () => {
    const enabled = Boolean(form.elements.create_follow_up?.checked);
    const fields = form.querySelector("[data-follow-up-fields]");
    if (!fields) return;
    fields.hidden = !enabled;
    fields.querySelectorAll("input, select").forEach((control) => { control.disabled = !enabled; });
    const schedule = form.elements.schedule_follow_up;
    const calendar = form.querySelector("[data-follow-up-calendar]");
    if (!schedule || !calendar) return;
    schedule.disabled = !enabled;
    if (!enabled) schedule.checked = false;
    calendar.hidden = !enabled || !schedule.checked;
    calendar.querySelector("select").disabled = calendar.hidden;
  };

  const showFields = (names) => {
    dialog.querySelectorAll("[data-field]").forEach((field) => {
      field.hidden = !names.includes(field.dataset.field);
      field.querySelectorAll("input, select, textarea").forEach((control) => { control.disabled = field.hidden; });
    });
  };

  const renderHistory = (events = []) => {
    const target = dialog.querySelector("[data-history-list]");
    target.replaceChildren();
    if (!events.length) {
      target.innerHTML = "<p>Nenhum evento registrado.</p>";
      return;
    }
    const list = document.createElement("ol");
    events.forEach((event) => {
      const item = document.createElement("li");
      const date = new Date(event.occurred_at).toLocaleString("pt-BR");
      const detail = event.details?.comment || event.details?.status || "";
      item.textContent = `${date} · ${event.type}${detail ? ` — ${detail}` : ""}`;
      list.append(item);
    });
    target.append(list);
  };

  const openDialog = async (card, mode) => {
    selectedCard = card;
    const config = modeConfig[mode];
    form.reset();
    form.elements.action_id.value = card.dataset.actionId;
    form.elements.expected_version.value = card.dataset.version;
    form.elements.mode.value = mode;
    form.elements.title.value = card.dataset.title || "";
    form.elements.owner_id.value = card.dataset.ownerId || "";
    form.elements.due_at.value = card.dataset.dueAt || "";
    dialog.querySelector("[data-dialog-title]").textContent = config.title;
    dialog.querySelector("[data-dialog-submit]").hidden = !config.submit;
    dialog.querySelector("[data-dialog-submit]").textContent = config.submit || "";
    dialog.querySelector("[data-note-label]").textContent = config.note || "Observação";
    dialog.querySelector("[data-dialog-feedback]").textContent = "";
    showFields(config.fields);
    if (mode === "complete") {
      form.elements.follow_up_owner_id.value = card.dataset.ownerId || "";
      syncFollowUp();
    }
    dialog.showModal();
    try {
      const detail = await api(`/api/v1/actions/${card.dataset.actionId}`);
      renderHistory(detail.events);
    } catch (error) {
      dialog.querySelector("[data-history-list]").textContent = error.message;
    }
  };

  page.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-action-open]");
    if (trigger) openDialog(trigger.closest("[data-action-card]"), trigger.dataset.actionOpen);
  });
  dialog?.querySelector("[data-dialog-cancel]")?.addEventListener("click", () => dialog.close());
  form?.elements.create_follow_up?.addEventListener("change", syncFollowUp);
  form?.elements.schedule_follow_up?.addEventListener("change", syncFollowUp);

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const mode = form.elements.mode.value;
    const id = form.elements.action_id.value;
    const version = Number(form.elements.expected_version.value);
    const note = form.elements.note.value.trim();
    const feedback = dialog.querySelector("[data-dialog-feedback]");
    try {
      let result;
      if (mode === "edit") {
        result = await api(`/api/v1/actions/${id}`, { method: "PATCH", body: JSON.stringify({
          title: form.elements.title.value.trim(), owner_id: form.elements.owner_id.value || null,
          due_at: form.elements.due_at.value || null, expected_version: version,
        }) });
      } else if (mode === "accept" || mode === "dismiss") {
        result = await api(`/api/v1/actions/${id}/decision`, { method: "POST", body: JSON.stringify({
          decision: mode, reason: mode === "dismiss" ? note : null,
          owner_id: form.elements.owner_id.value || null, expected_version: version,
          due_at: mode === "accept" ? form.elements.due_at.value || null : undefined,
        }) });
      } else if (mode === "comment") {
        result = await api(`/api/v1/actions/${id}/comments`, { method: "POST", body: JSON.stringify({ comment: note, expected_version: version }) });
      } else {
        const followUp = mode === "complete" && form.elements.create_follow_up.checked ? {
          title: form.elements.follow_up_title.value.trim(),
          owner_id: form.elements.follow_up_owner_id.value,
          due_at: form.elements.follow_up_due_at.value,
          calendar_provider: form.elements.schedule_follow_up?.checked
            ? form.elements.follow_up_calendar_provider.value
            : null,
        } : null;
        result = await api(`/api/v1/actions/${id}`, { method: "PATCH", body: JSON.stringify({
          status: mode === "start" ? "in_progress" : "completed",
          outcome: mode === "complete" ? note : null, expected_version: version,
          customer_notified: mode === "complete" ? form.elements.customer_notified.checked : undefined,
          follow_up: followUp,
        }) });
      }
      selectedCard.dataset.version = result.version;
      pageFeedback.textContent = "Ação atualizada com sucesso.";
      dialog.close();
      window.location.reload();
    } catch (error) {
      feedback.textContent = error.code === "version_conflict"
        ? "Outra pessoa alterou esta ação. Feche e abra novamente para atualizar."
        : error.message;
    }
  });
}

const feedbackSection = document.querySelector("[data-analysis-feedback]");
if (feedbackSection) {
  api("/api/v1/telemetry/events", {
    method: "POST",
    body: JSON.stringify({ event_name: "result_opened", target_type: "analysis", target_id: feedbackSection.dataset.analysisId }),
  }).catch(() => {});
  feedbackSection.querySelector("[data-feedback-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const status = feedbackSection.querySelector("[data-feedback-status]");
    try {
      const result = await api(`/api/v1/analyses/${feedbackSection.dataset.analysisId}/feedback`, {
        method: "POST",
        body: JSON.stringify({
          usefulness_rating: Number(form.get("usefulness_rating")),
          factual_accuracy: form.get("factual_accuracy"),
          action_useful: form.get("action_useful") === "true",
          correction: form.get("correction"),
          comment: form.get("comment"),
        }),
      });
      status.textContent = "Feedback salvo.";
    } catch (error) { status.textContent = error.message; }
  });
}
