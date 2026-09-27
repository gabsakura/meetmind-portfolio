const page = document.querySelector("[data-ai-workspace]");
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const notice = document.querySelector("#aviso-global");

function showNotice(message, kind = "info") {
  if (!notice) return;
  notice.textContent = message;
  notice.className = `aviso aviso-${kind}`;
  notice.hidden = false;
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) headers.set("X-CSRFToken", csrfToken);
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({ error: "Resposta inválida." }));
  if (!response.ok) throw new Error(payload.error || "Não foi possível concluir.");
  return payload;
}

if (page) {
  const select = page.querySelector("#ai-meeting-select");
  const processing = page.querySelector("#ai-processing");
  const empty = page.querySelector("#ai-empty");
  const resultBox = page.querySelector("#ai-result");
  const analysisButton = page.querySelector("#ai-analysis-button");
  let requestSequence = 0;
  const maxPollAttempts = 150;

  const labels = {
    objective: { achieved: "Atingido", partial: "Parcial", not_achieved: "Não atingido", unclear: "Inconclusivo" },
    "follow-up": { scheduled: "Agendado", in_person: "Presencial confirmado", proposed: "Proposto", none: "Não registrado", unclear: "Inconclusivo" },
    conflict: { none_detected: "Sem conflito explícito", tension: "Tensão identificada", argument: "Argumento identificado", unclear: "Inconclusivo" },
  };
  const tones = {
    achieved: "positive", scheduled: "positive", in_person: "positive", none_detected: "neutral",
    partial: "attention", proposed: "attention", tension: "attention",
    not_achieved: "risk", argument: "risk", unclear: "neutral", none: "neutral",
  };

  function evidenceIds(value, found = new Set()) {
    if (Array.isArray(value)) {
      value.forEach((item) => evidenceIds(item, found));
    } else if (value && typeof value === "object") {
      if (value.segment_id) found.add(String(value.segment_id));
      Object.values(value).forEach((item) => evidenceIds(item, found));
    }
    return found;
  }

  function uniqueText(values = []) {
    return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))];
  }

  function readingOverview(result, payload) {
    const isModes = result.schema_version === "meetmind-modes-v1";
    const modeResults = result.mode_results || {};
    const productMode = modeResults.product_operations || {};
    const relationshipMode = modeResults.relationship_risk || {};
    const classification = result.meeting_classification || {};
    const routing = result.routing || {};
    const health = result.relationship_health || {};
    const brief = result.manufacturing_brief || {};
    const products = uniqueText(
      isModes
        ? (productMode.product_mentions || []).map(
            (item) => item.canonical_name || item.original_term
          )
        : result.products_mentioned || []
    );

    const meetingType = classification.meeting_type || routing.meeting_type;
    const commercialFindings = (result.findings || []).filter(
      (item) => item.category === "commercial"
    );
    const saleEvidence = new Set([
      ...(classification.evidence_segment_ids || []),
      ...evidenceIds(commercialFindings),
      ...evidenceIds(routing),
    ]);
    const hasSaleSignal = meetingType === "commercial_expansion" && saleEvidence.size > 0;

    const churnStrength = relationshipMode.churn_signal_strength;
    let churnStatus = "Inconclusivo";
    let churnTone = "neutral";
    let churnDetail = "Não há evidência suficiente para avaliar uma ameaça de churn.";
    if (churnStrength) {
      const churnLabels = {
        absent: "Não identificado",
        weak: "Sinal fraco",
        moderate: "Sinal moderado",
        strong: "Sinal forte",
      };
      churnStatus = churnLabels[churnStrength] || "Inconclusivo";
      churnTone = ["moderate", "strong"].includes(churnStrength)
        ? "risk"
        : churnStrength === "weak"
          ? "attention"
          : "positive";
      churnDetail = `${relationshipMode.rationale || "Leitura qualitativa do relacionamento."} Não representa probabilidade de churn.`;
    } else if (health.status) {
      const healthMap = {
        healthy: ["Não identificado", "positive"],
        attention: ["Sinal de atenção", "attention"],
        at_risk: ["Ameaça qualitativa", "risk"],
        insufficient_evidence: ["Inconclusivo", "neutral"],
      };
      [churnStatus, churnTone] = healthMap[health.status] || ["Inconclusivo", "neutral"];
      churnDetail = `${health.rationale || "Sem justificativa registrada."} Não representa probabilidade de churn.`;
    }

    const genericSpeakers = new Set([
      "cliente", "customer", "participante", "participante 1", "speaker",
      "desconhecido", "unknown",
    ]);
    const segmentSpeakers = (payload.segments || [])
      .filter((segment) => segment.is_totvs === false)
      .map((segment) => segment.speaker);
    const requestSpeakers = (brief.customer_requests || []).flatMap((request) =>
      (request.evidence || []).map((evidence) => evidence.speaker)
    );
    const personas = uniqueText([...segmentSpeakers, ...requestSpeakers]).filter(
      (speaker) => !genericSpeakers.has(speaker.toLocaleLowerCase("pt-BR"))
    );

    const budgetPattern =
      /\b(budget|or[cç]amento|verba|limite de investimento|valor aprovado|aprova[cç][aã]o financeira)\b/i;
    const budgetCandidates = [
      ...(result.findings || []).filter((item) =>
        ["financial", "commercial"].includes(item.category)
      ),
      ...(modeResults.voc_complaint?.complaints || []).filter((item) =>
        ["contract", "licensing"].includes(item.category)
      ),
    ].filter((item) => {
      const candidateText = [
        item.title,
        item.observation,
        item.interpretation,
        item.business_impact,
        item.impact,
        item.desired_outcome,
      ].filter(Boolean).join(" ");
      return budgetPattern.test(candidateText) && evidenceIds(item).size > 0;
    });
    const budget = budgetCandidates[0];
    const sentimentLabels = {
      positive: ["Positivo", "positive", "Predominam sinais positivos na conversa."],
      neutral: ["Neutro", "neutral", "Não houve predominância clara de sinais positivos ou negativos."],
      negative: ["Negativo", "risk", "Predominam sinais negativos na conversa."],
      mixed: ["Misto", "attention", "A conversa reuniu sinais positivos e negativos."],
    };
    const sentiment = sentimentLabels[result.overall_sentiment] || [
      "Não classificado",
      "neutral",
      "Esta versão da análise não registrou o sentimento geral.",
    ];

    return [
      {
        title: "Produto identificado",
        icon: "▦",
        status: products.length ? "Identificado" : "Não confirmado",
        detail: products.length
          ? products.slice(0, 3).join(", ")
          : "Nenhum produto foi explicitamente confirmado na leitura.",
        tone: products.length ? "positive" : "neutral",
        evidenceCount: evidenceIds(productMode.product_mentions || []).size,
      },
      {
        title: "Sinal de venda",
        icon: "↗",
        status: hasSaleSignal ? "Sinal explícito" : "Não confirmado",
        detail: hasSaleSignal
          ? "A finalidade foi classificada como expansão comercial com evidência."
          : "Não há expansão comercial explícita; continuidade não é tratada como venda.",
        tone: hasSaleSignal ? "positive" : "neutral",
        evidenceCount: hasSaleSignal ? saleEvidence.size : 0,
      },
      {
        title: "Ameaça de churn",
        icon: "!",
        status: churnStatus,
        detail: churnDetail,
        tone: churnTone,
        evidenceCount: evidenceIds(relationshipMode.evidence || []).size ||
          (health.evidence_segment_ids || []).length,
      },
      {
        title: "Persona identificada",
        icon: "◎",
        status: personas.length ? "Identificada" : "Não identificada",
        detail: personas.length
          ? `Participante(s) atribuído(s) ao cliente: ${personas.slice(0, 3).join(", ")}.`
          : "O papel profissional do participante não foi explicitamente identificado.",
        tone: personas.length ? "positive" : "neutral",
        evidenceCount: 0,
      },
      {
        title: "Budget do cliente",
        icon: "$",
        status: budget ? "Mencionado" : "Não mencionado",
        detail: budget
          ? budget.observation || budget.impact || "Há referência financeira explícita."
          : "Não foi encontrado valor, verba ou restrição orçamentária explícita.",
        tone: budget ? "attention" : "neutral",
        evidenceCount: budget ? evidenceIds(budget).size : 0,
      },
      {
        title: "Sentimento geral",
        icon: "◡",
        status: sentiment[0],
        detail: sentiment[2],
        tone: sentiment[1],
        evidenceCount: 0,
      },
    ];
  }

  function renderReadingOverview(result, payload) {
    const list = page.querySelector("#ai-reading-card-grid");
    if (!list) return;
    list.replaceChildren();
    readingOverview(result, payload).forEach((item) => {
      const card = document.createElement("article");
      card.className = `ai-reading-card ai-reading-card-${item.tone}`;
      card.title = item.detail;
      const top = document.createElement("div");
      top.className = "ai-reading-card-top";
      const icon = document.createElement("span");
      icon.className = "ai-reading-card-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = item.icon;
      top.append(icon);
      const label = document.createElement("span");
      label.textContent = item.title;
      const status = document.createElement("strong");
      status.textContent = item.status;
      const heading = document.createElement("div");
      heading.className = "ai-reading-card-heading";
      heading.append(label, status);
      top.append(heading);
      if (item.evidenceCount > 0) {
        const evidence = document.createElement("small");
        evidence.textContent = `${item.evidenceCount} evid.`;
        top.append(evidence);
      }
      const detail = document.createElement("p");
      detail.textContent = item.detail;
      card.append(top, detail);
      list.append(card);
    });
  }

  function selectedOption() { return select?.selectedOptions?.[0]; }
  function setLoading(active) {
    processing.hidden = !active;
    if (active) empty.hidden = true;
    if (analysisButton) analysisButton.disabled = active;
  }
  function setSignal(name, decision = {}) {
    const status = decision.status || "unclear";
    const card = page.querySelector(`#ai-${name}-card`);
    card.className = `ai-signal ai-signal-${tones[status] || "neutral"}`;
    page.querySelector(`#ai-${name}-status`).textContent = labels[name]?.[status] || "Inconclusivo";
    page.querySelector(`#ai-${name}-reason`).textContent = decision.rationale || "Sem evidência suficiente para concluir.";
  }
  function legacyBrief(result) {
    const summary = result.executive_summary || {};
    return {
      what_happened: summary.what_happened || "A análise anterior não registrou uma leitura operacional.",
      customer_requests: [],
      objective: { status: "unclear", rationale: "Campo indisponível nesta versão." },
      follow_up: { status: "unclear", rationale: "Campo indisponível nesta versão." },
      conflict: { status: "unclear", rationale: "Campo indisponível nesta versão." },
      concern: {
        status: ["high", "critical"].includes(result.risk_level) ? "attention" : "unclear",
        rationale: result.relationship_health?.rationale || "Campo indisponível nesta versão.",
        evidence: [],
      },
      suggested_actions: result.action_items || [],
    };
  }
  function renderRequests(requests = []) {
    const list = page.querySelector("#ai-request-list");
    list.replaceChildren();
    requests.forEach((item) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = item.request;
      const detail = document.createElement("p");
      detail.textContent = item.target ? `Relacionado a: ${item.target}` : "Pedido confirmado na conversa.";
      card.append(title, detail);
      list.append(card);
    });
    if (!list.children.length) {
      const message = document.createElement("p");
      message.className = "texto-secundario";
      message.textContent = "Nenhum pedido explícito do cliente foi confirmado.";
      list.append(message);
    }
  }
  const actionStatusLabels = {
    proposed: "Aguardando revisão",
    accepted: "Pendente",
    in_progress: "Em andamento",
    completed: "Concluída",
    dismissed: "Descartada",
  };

  function findingBySegment(result) {
    const catalog = new Map();
    (result.findings || []).forEach((finding) => {
      (finding.evidence || []).forEach((evidence) => {
        catalog.set(String(evidence.segment_id), {
          label: finding.title || finding.trigger?.label || "Evidência acionável",
          category: finding.trigger?.trigger_type || finding.category || "commitment",
          confidence: finding.confidence,
        });
      });
    });
    return catalog;
  }

  function renderEvidence(payload, result) {
    const list = page.querySelector("#ai-evidence-list");
    const catalog = findingBySegment(result);
    const unique = new Map();
    (payload.actions || []).forEach((action) => {
      (action.evidence || []).forEach((evidence) => {
        if (!unique.has(evidence.segment_id)) {
          unique.set(evidence.segment_id, { ...evidence, action, finding: catalog.get(String(evidence.segment_id)) });
        }
      });
    });
    list.replaceChildren();
    unique.forEach((item) => {
      const category = item.finding?.category || (item.action.priority === "high" ? "commercial_risk" : "commitment");
      const tone = ["commercial_risk", "dissatisfaction", "operational_problem"].includes(category)
        ? "risk"
        : category === "opportunity" ? "positive" : "attention";
      const card = document.createElement("article");
      card.className = `ai-evidence-card ai-evidence-card-${tone}`;
      const heading = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = item.finding?.label || item.action.rationale || "Compromisso identificado";
      const certainty = document.createElement("span");
      certainty.textContent = typeof item.finding?.confidence === "number"
        ? `${Math.round(item.finding.confidence * 100)}% de confiança`
        : "Citação validada";
      heading.append(title, certainty);
      const quote = document.createElement("blockquote");
      quote.textContent = `“${item.quote}”`;
      const meta = document.createElement("p");
      meta.textContent = `${item.speaker || "Participante não identificado"} · segmento ${item.segment_id}`;
      const actions = document.createElement("div");
      const auditLink = document.createElement("a");
      auditLink.href = `/auditabilidade-ia?meeting=${payload.meeting_id}&analysis=${payload.id}#segment-${encodeURIComponent(item.segment_id)}`;
      auditLink.textContent = "Abrir trecho";
      const report = document.createElement("button");
      report.type = "button";
      report.className = "link-button";
      report.textContent = "Sinalizar erro";
      report.hidden = page.dataset.canRequest !== "true";
      report.addEventListener("click", async () => {
        const comment = window.prompt("Descreva o problema desta evidência:");
        if (!comment) return;
        try {
          await apiFetch(`/api/v1/analyses/${payload.id}/evidence-issues`, {
            method: "POST",
            body: JSON.stringify({ segment_id: item.segment_id, category: "incorrect", comment }),
          });
          showNotice("Problema de evidência registrado.", "success");
        } catch (error) {
          showNotice(error.message, "error");
        }
      });
      actions.append(auditLink, report);
      card.append(heading, quote, meta, actions);
      list.append(card);
    });
    if (!list.children.length) {
      const message = document.createElement("p");
      message.className = "texto-secundario";
      message.textContent = "Nenhuma evidência acionável foi confirmada nesta reunião.";
      list.append(message);
    }
  }

  function renderTranscript(payload, result) {
    const panel = page.querySelector("#ai-transcript-panel");
    const list = page.querySelector("#ai-transcript-list");
    const search = page.querySelector("#ai-transcript-search");
    const coverage = page.querySelector("#ai-transcript-coverage");
    if (!panel || !list || !search || !coverage) return;

    const segments = payload.segments || [];
    const cited = evidenceIds(result);
    const validIds = new Set(segments.map((segment) => String(segment.id)));
    const citedCount = [...cited].filter((id) => validIds.has(id)).length;
    coverage.textContent = `${citedCount} de ${segments.length} trechos citados`;
    panel.hidden = !segments.length;

    const render = () => {
      const query = search.value.trim().toLocaleLowerCase("pt-BR");
      const visible = segments.filter((segment) =>
        `${segment.speaker || ""} ${segment.text || ""}`
          .toLocaleLowerCase("pt-BR")
          .includes(query)
      );
      list.replaceChildren();
      visible.forEach((segment) => {
        const item = document.createElement("article");
        item.id = `analysis-segment-${segment.id}`;
        item.className = `ai-transcript-segment${cited.has(String(segment.id)) ? " is-cited" : ""}`;
        const heading = document.createElement("header");
        const speaker = document.createElement("strong");
        speaker.textContent = segment.speaker || "Participante não identificado";
        const marker = document.createElement("span");
        marker.textContent = cited.has(String(segment.id)) ? "Citado pela análise" : `Trecho ${segment.position + 1}`;
        const text = document.createElement("p");
        text.textContent = segment.text;
        heading.append(speaker, marker);
        item.append(heading, text);
        list.append(item);
      });
      if (!visible.length) {
        const emptyMessage = document.createElement("p");
        emptyMessage.className = "texto-secundario";
        emptyMessage.textContent = "Nenhum trecho corresponde à busca.";
        list.append(emptyMessage);
      }
    };
    search.value = "";
    search.oninput = render;
    render();
  }
  function renderPlan(payload) {
    const plan = payload.plan;
    const content = page.querySelector("#ai-plan-content");
    const status = page.querySelector("#ai-plan-status");
    content.replaceChildren();
    if (!plan) {
      status.textContent = "";
      const message = document.createElement("p");
      message.className = "texto-secundario";
      message.textContent = "Nenhuma ação recomendada.";
      content.append(message);
      return;
    }
    status.textContent = {
      awaiting_review: "Aguardando revisão",
      active: "Ativo",
      completed: "Concluído",
      dismissed: "Descartado",
    }[plan.status] || plan.status;
    [
      ["Objetivo", plan.objective],
      ["Situação", plan.situation],
      ["Resposta recomendada", plan.recommendation],
    ].forEach(([label, value]) => {
      const item = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = label;
      const text = document.createElement("p");
      text.textContent = value;
      item.append(title, text);
      content.append(item);
    });
  }

  function actionEditor(action, payload) {
    const card = document.createElement("article");
    card.className = `ai-action-review ai-action-review-${action.priority || "low"}`;
    card.dataset.actionId = action.id;
    card.dataset.version = action.version;
    const heading = document.createElement("div");
    const badge = document.createElement("span");
    badge.textContent = actionStatusLabels[action.status] || action.status;
    const priority = document.createElement("small");
    priority.textContent = { high: "Alta", medium: "Média", low: "Baixa" }[action.priority] || "Baixa";
    heading.append(badge, priority);
    const title = document.createElement("input");
    title.name = "title";
    title.value = action.title;
    title.disabled = action.status !== "proposed";
    title.setAttribute("aria-label", "Título da ação");
    const rationale = document.createElement("p");
    rationale.textContent = action.rationale;
    const fields = document.createElement("div");
    fields.className = "ai-action-fields";
    const ownerLabel = document.createElement("label");
    ownerLabel.textContent = "Responsável";
    const owner = document.createElement("select");
    owner.name = "owner_id";
    owner.disabled = action.status !== "proposed";
    owner.innerHTML = '<option value="">Selecione</option>';
    (payload.action_assignees || []).forEach((user) => {
      const option = document.createElement("option");
      option.value = user.id;
      option.textContent = user.name;
      option.selected = user.id === action.owner_id;
      owner.append(option);
    });
    ownerLabel.append(owner);
    const dueLabel = document.createElement("label");
    dueLabel.textContent = "Prazo";
    const due = document.createElement("input");
    due.name = "due_at";
    due.type = "date";
    due.disabled = action.status !== "proposed";
    due.value = action.due_at ? action.due_at.slice(0, 10) : "";
    dueLabel.append(due);
    fields.append(ownerLabel, dueLabel);
    card.append(heading, title, rationale, fields);
    if (action.status === "proposed" && page.dataset.canRequest === "true") {
      const controls = document.createElement("div");
      controls.className = "ai-action-controls";
      const accept = document.createElement("button");
      accept.type = "button";
      accept.className = "botao-primario";
      accept.textContent = "Confirmar";
      accept.addEventListener("click", async () => {
        try {
          await apiFetch(`/api/v1/actions/${action.id}/decision`, {
            method: "POST",
            body: JSON.stringify({
              decision: "accept",
              title: title.value.trim(),
              owner_id: owner.value || null,
              due_at: due.value || null,
              expected_version: action.version,
            }),
          });
          renderResult(await apiFetch(`/api/v1/analyses/${payload.id}`));
          showNotice("Ação confirmada.", "success");
        } catch (error) {
          page.querySelector("#ai-action-feedback").textContent = error.message;
        }
      });
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "botao-secundario";
      dismiss.textContent = "Descartar";
      dismiss.addEventListener("click", async () => {
        const reason = window.prompt("Por que esta ação não deve ser executada?");
        if (!reason) return;
        try {
          await apiFetch(`/api/v1/actions/${action.id}/decision`, {
            method: "POST",
            body: JSON.stringify({ decision: "dismiss", reason, expected_version: action.version }),
          });
          renderResult(await apiFetch(`/api/v1/analyses/${payload.id}`));
          showNotice("Ação descartada.", "success");
        } catch (error) {
          page.querySelector("#ai-action-feedback").textContent = error.message;
        }
      });
      controls.append(accept, dismiss);
      card.append(controls);
    }
    return card;
  }

  function renderActions(payload) {
    const list = page.querySelector("#ai-action-list");
    const confirmAll = page.querySelector("#ai-confirm-all");
    const actions = payload.actions || [];
    list.replaceChildren();
    actions.forEach((action) => list.append(actionEditor(action, payload)));
    const drafts = actions.filter((action) => action.status === "proposed");
    confirmAll.hidden = !drafts.length || page.dataset.canRequest !== "true";
    confirmAll.onclick = async () => {
      const entries = drafts.map((action) => {
        const card = list.querySelector(`[data-action-id="${action.id}"]`);
        return {
          id: action.id,
          expected_version: action.version,
          title: card.querySelector('[name="title"]').value.trim(),
          owner_id: card.querySelector('[name="owner_id"]').value || null,
          due_at: card.querySelector('[name="due_at"]').value || null,
        };
      });
      if (entries.some((entry) => !entry.title || !entry.owner_id || !entry.due_at)) {
        page.querySelector("#ai-action-feedback").textContent = "Defina título, responsável e prazo de todas as ações.";
        return;
      }
      try {
        await apiFetch(`/api/v1/analyses/${payload.id}/actions/confirm-all`, {
          method: "POST",
          body: JSON.stringify({ actions: entries }),
        });
        renderResult(await apiFetch(`/api/v1/analyses/${payload.id}`));
        showNotice("Todas as ações foram confirmadas.", "success");
      } catch (error) {
        page.querySelector("#ai-action-feedback").textContent = error.message;
      }
    };
    if (!actions.length) {
      const message = document.createElement("p");
      message.className = "texto-secundario";
      message.textContent = "Nenhuma ação recomendada.";
      list.append(message);
    }
  }
  async function loadParticipants() {
    if (!select?.value) return;
    const panel = page.querySelector("[data-ai-participants]");
    if (!panel) return;
    const payload = await apiFetch("/api/v1/meetings/" + select.value + "/participants");
    const meta = panel.querySelector("[data-participant-meta]");
    const list = panel.querySelector("[data-participant-list]");
    meta.textContent = [
      payload.client_owner ? "Responsável: " + payload.client_owner.name : "Responsável não definido",
      payload.creator ? "Enviada por: " + payload.creator.name : "",
    ].filter(Boolean).join(" · ");
    list.replaceChildren();
    payload.items.forEach((item) => {
      const row = document.createElement("span");
      row.className = "ai-participant-chip";
      row.append(document.createTextNode(item.name));
      if (payload.can_manage && item.user_id !== payload.creator?.id) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.dataset.removeParticipant = item.user_id;
        remove.setAttribute("aria-label", "Remover " + item.name);
        remove.textContent = "×";
        row.append(remove);
      }
      list.append(row);
    });
    const form = panel.querySelector("[data-participant-form]");
    if (form) form.hidden = !payload.can_manage;
  }
  function renderResult(payload) {
    const result = payload.result || {};
    const isCurrent = result.schema_version === "meetmind-manufacturing-v1" && result.manufacturing_brief;
    const brief = isCurrent ? result.manufacturing_brief : legacyBrief(result);
    renderReadingOverview(result, payload);
    renderEvidence(payload, result);
    renderPlan(payload);
    renderTranscript(payload, result);
    const option = selectedOption();
    page.querySelector("#ai-meeting-id").textContent = `REUNIÃO ${option?.dataset.externalId || ""}`;
    page.querySelector("#ai-client-name").textContent = option?.dataset.client || "Cliente não identificado";
    const type = result.meeting_classification?.meeting_type || "tipo não definido";
    page.querySelector("#ai-meeting-meta").textContent = `${option?.dataset.title || ""} · ${option?.dataset.date || ""} · ${type.replaceAll("_", " ")}`;
    page.querySelector("#ai-what-happened").textContent = brief.what_happened;
    setSignal("objective", brief.objective);
    setSignal("follow-up", brief.follow_up);
    setSignal("conflict", brief.conflict);
    renderRequests(brief.customer_requests);
    renderActions(payload);
    const concern = brief.concern || {};
    const concernBox = page.querySelector("#ai-concern");
    const concernVisible = ["attention", "high"].includes(concern.status) && (concern.evidence || []).length > 0;
    concernBox.hidden = !concernVisible;
    if (concernVisible) {
      concernBox.className = `ai-concern ai-concern-${concern.status}`;
      page.querySelector("#ai-concern-title").textContent = concern.status === "high" ? "Sinal importante identificado" : "Ponto de atenção identificado";
      page.querySelector("#ai-concern-summary").textContent = concern.rationale;
    }
    page.querySelector("#ai-legacy-warning").hidden = isCurrent;
    const auditLink = page.querySelector("#ai-audit-link");
    if (auditLink) auditLink.href = `/auditabilidade-ia?meeting=${payload.meeting_id}&analysis=${payload.id}`;
    setLoading(false);
    empty.hidden = true;
    resultBox.hidden = false;
  }
  async function poll(url, sequence, attempt = 0) {
    const payload = await apiFetch(url);
    if (sequence !== requestSequence) return;
    if (payload.status === "succeeded") {
      renderResult(payload);
      showNotice("Análise concluída.", "success");
    } else if (payload.status === "failed") {
      setLoading(false);
      showNotice("A análise não foi concluída. Consulte a auditabilidade para o diagnóstico.", "error");
    } else if (attempt >= maxPollAttempts) {
      setLoading(false);
      showNotice("A análise continua na fila. Você pode sair desta página e consultar o resultado depois.", "info");
    } else {
      window.setTimeout(() => {
        poll(url, sequence, attempt + 1).catch((error) => {
          setLoading(false);
          showNotice(error.message, "error");
        });
      }, 2000);
    }
  }
  async function loadAvailableResult() {
    if (!select?.value) return;
    const sequence = ++requestSequence;
    setLoading(false);
    resultBox.hidden = true;
    empty.hidden = false;
    try {
      await loadParticipants();
      const selectedAnalysis = page.dataset.selectedAnalysis;
      if (selectedAnalysis) {
        page.dataset.selectedAnalysis = "";
        const payload = await apiFetch(`/api/v1/analyses/${selectedAnalysis}`);
        if (sequence === requestSequence && payload.meeting_id === select.value) {
          if (payload.status === "succeeded") {
            renderResult(payload);
          } else if (payload.status === "failed") {
            showNotice("A análise não foi concluída. Consulte a auditabilidade para o diagnóstico.", "error");
          } else {
            setLoading(true);
            await poll(`/api/v1/analyses/${selectedAnalysis}`, sequence, 0);
          }
        }
        return;
      }
      const history = await apiFetch(`/api/v1/meetings/${select.value}/analyses`);
      if (sequence !== requestSequence) return;
      const previous = history.items?.find((item) => item.status === "succeeded");
      if (previous) renderResult(await apiFetch(previous.status_url));
    } catch (error) { showNotice(error.message, "error"); }
  }

  select?.addEventListener("change", loadAvailableResult);
  page.querySelector("[data-participant-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const feedback = page.querySelector("[data-participant-feedback]");
    try {
      await apiFetch("/api/v1/meetings/" + select.value + "/participants", {
        method: "POST",
        body: JSON.stringify({ user_id: form.elements.user_id.value, role: "participant" }),
      });
      form.reset();
      await loadParticipants();
      feedback.textContent = "Participante adicionado.";
    } catch (error) { feedback.textContent = error.message; }
  });
  page.querySelector("[data-participant-list]")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-remove-participant]");
    if (!button) return;
    const feedback = page.querySelector("[data-participant-feedback]");
    try {
      await apiFetch(
        "/api/v1/meetings/" + select.value + "/participants/" + button.dataset.removeParticipant,
        { method: "DELETE" },
      );
      await loadParticipants();
      feedback.textContent = "Participante removido.";
    } catch (error) { feedback.textContent = error.message; }
  });
  analysisButton?.addEventListener("click", async () => {
    try {
      const sequence = ++requestSequence;
      setLoading(true);
      const created = await apiFetch(`/api/v1/meetings/${select.value}/analyses`, { method: "POST" });
      await poll(created.status_url, sequence, 0);
    } catch (error) { setLoading(false); showNotice(error.message, "error"); }
  });
  loadAvailableResult();
}
