const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const notice = document.querySelector("#aviso-global");

function showNotice(message, kind = "info") {
  if (!notice) return;
  notice.textContent = message;
  notice.className = `aviso aviso-${kind}`;
  notice.hidden = false;
  notice.focus?.();
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
    headers.set("X-CSRFToken", csrfToken);
  }
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({ error: "Resposta inválida." }));
  if (!response.ok) throw new Error(payload.error || "Não foi possível concluir.");
  return payload;
}

const search = document.querySelector("[data-table-search]");
if (search) {
  search.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase("pt-BR");
    document.querySelectorAll("[data-search-table] tbody tr").forEach((row) => {
      row.hidden = !row.textContent.toLocaleLowerCase("pt-BR").includes(query);
    });
  });
}

const analysisPage = document.querySelector("[data-analysis-page]");
if (analysisPage) {
  const select = document.querySelector("#meeting-select");
  const analysisButton = document.querySelector("#analysis-button");
  const processing = document.querySelector("#analysis-state");
  const resultBox = document.querySelector("#analysis-result");
  const emptyState = document.querySelector("#ai-empty");
  if (emptyState) {
    const syncEmptyState = () => {
      emptyState.hidden = !(resultBox.hidden && processing.hidden);
    };
    const observer = new MutationObserver(syncEmptyState);
    observer.observe(resultBox, { attributeFilter: ["hidden"] });
    observer.observe(processing, { attributeFilter: ["hidden"] });
    syncEmptyState();
  }
  let selectedMeetingRequest = 0;

  const modeLabels = {
    voc_complaint: "Reclamações e VoC",
    meeting_outcome: "Finalidade e desfecho",
    product_operations: "Produtos e operação",
    relationship_risk: "Risco de relacionamento",
  };
  const meetingTypeLabels = {
    idea_alignment: "alinhamento de ideias",
    solution_presentation: "apresentação de solução",
    technical_diagnosis: "diagnóstico técnico",
    questions_clarifications: "dúvidas e esclarecimentos",
    contract_negotiation: "negociação contratual",
    problem_resolution: "resolução de problema",
    unknown: "não definido",
  };

  const analysisErrors = {
    ai_not_configured: "A chave Gemini não chegou ao worker.",
    ai_request_rejected: "O Gemini rejeitou a requisição. Verifique a chave, o projeto e o modelo.",
    ai_auth_rejected: "A chave Gemini não foi autorizada pelo provedor.",
    ai_model_unavailable: "O modelo configurado não está disponível para esta chave.",
    ai_temporarily_unavailable: "O Gemini está temporariamente indisponível ou sem cota.",
    ai_provider_error: "O provedor de IA retornou um erro.",
    invalid_model_output: "A resposta da IA não passou pelas validações de rastreabilidade.",
    ai_processing_error: "O worker não conseguiu processar a resposta da IA.",
    pii_detected: "A análise foi bloqueada porque a transcrição contém dado pessoal sensível.",
    queue_unavailable: "A fila Redis está indisponível.",
  };

  function analysisErrorMessage(code) {
    if (code?.startsWith("invalid_model_output_invalid_structured_output")) {
      const technical = code
        .replace("invalid_model_output_invalid_structured_output:", "")
        .replaceAll("_", " ");
      return (
        "A resposta da IA não respeitou o formato estruturado exigido. " +
        `Detalhe técnico: ${technical || "não informado"}.`
      );
    }
    if (code?.startsWith("invalid_model_output_unknown_evidence_segment")) {
      return "A IA citou um segmento que não existe nesta reunião.";
    }
    if (code?.startsWith("invalid_model_output_unknown_action_evidence_segment")) {
      return "Uma ação da IA apontou para um segmento inexistente.";
    }
    if (code?.startsWith("invalid_model_output_evidence_quote_not_literal")) {
      return "Uma citação da IA não pôde ser confirmada literalmente na transcrição.";
    }
    if (code?.startsWith("invalid_model_output_")) {
      return analysisErrors.invalid_model_output;
    }
    return analysisErrors[code] || `A análise não foi concluída (${code || "erro controlado"}).`;
  }

  async function loadLatestAnalysis() {
    if (!select?.value) return;
    const requestNumber = ++selectedMeetingRequest;
    resultBox.hidden = true;
    processing.hidden = true;
    try {
      const explicitAnalysis = analysisPage.dataset.selectedAnalysis;
      if (explicitAnalysis) {
        analysisPage.dataset.selectedAnalysis = "";
        const payload = await apiFetch(`/api/v1/analyses/${explicitAnalysis}`);
        if (requestNumber === selectedMeetingRequest && payload.meeting_id === select.value) {
          renderResult(payload);
          showNotice("Análise selecionada carregada.", "success");
        }
        return;
      }
      const history = await apiFetch(`/api/v1/meetings/${select.value}/analyses`);
      if (requestNumber !== selectedMeetingRequest) return;
      const latest = history.items?.[0];
      if (!latest) return;
      if (latest.status === "queued" || latest.status === "running") {
        processing.hidden = false;
        showNotice("Esta reunião já possui uma análise em processamento.", "info");
        await poll(latest.status_url, requestNumber);
        return;
      }
      if (latest.status === "failed") {
        showNotice(analysisErrorMessage(latest.error), "error");
        return;
      }
      const payload = await apiFetch(latest.status_url);
      if (requestNumber === selectedMeetingRequest) {
        renderResult(payload);
        showNotice("Última análise concluída carregada.", "success");
      }
    } catch (error) {
      if (requestNumber === selectedMeetingRequest) showNotice(error.message, "error");
    }
  }

  select?.addEventListener("change", loadLatestAnalysis);


  function appendModeField(list, label, value) {
    if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return;
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = Array.isArray(value) ? value.join("; ") : String(value);
    list.append(term, detail);
  }

  function appendModeEvidence(container, evidence = []) {
    if (!evidence.length) return;
    const box = document.createElement("div");
    box.className = "modo-evidencias";
    evidence.forEach((item) => {
      const quote = document.createElement("blockquote");
      const text = document.createElement("p");
      text.textContent = `“${item.quote || ""}”`;
      const source = document.createElement("footer");
      source.textContent = `${item.speaker ? `${item.speaker} · ` : ""}segmento ${item.segment_id || "não informado"}`;
      quote.append(text, source);
      box.append(quote);
    });
    container.append(box);
  }

  function renderModeCard(mode, data) {
    const card = document.createElement("article");
    card.className = "modo-card";
    const title = document.createElement("h4");
    title.textContent = modeLabels[mode] || mode;
    card.append(title);
    const list = document.createElement("dl");

    if (mode === "voc_complaint") {
      appendModeField(list, "Síntese", data.summary);
      appendModeField(list, "Reclamações válidas", data.complaints?.length || 0);
      (data.complaints || []).forEach((item, index) => {
        appendModeField(list, `Reclamação ${index + 1}`, `${item.observation} — Impacto: ${item.impact}`);
        appendModeEvidence(card, item.evidence || []);
      });
    } else if (mode === "meeting_outcome") {
      appendModeField(list, "Tipo", meetingTypeLabels[data.meeting_type] || data.meeting_type);
      appendModeField(list, "Status do objetivo", data.objective_status);
      appendModeField(list, "Próximo encontro", data.follow_up_status);
      appendModeField(list, "Finalidade do próximo encontro", data.follow_up_purpose);
      appendModeField(list, "Interpretação", data.interpretation);
      appendModeField(list, "Compromissos", (data.commitments || []).map((item) => item.action));
    } else if (mode === "product_operations") {
      appendModeField(list, "Síntese", data.summary);
      appendModeField(list, "Produtos", (data.product_mentions || []).map((item) => item.canonical_name || item.original_term));
      appendModeField(list, "Sintomas", data.symptoms);
      appendModeField(list, "Processos afetados", data.impacted_processes);
      appendModeField(list, "Próximo diagnóstico", data.next_diagnostic_step);
      appendModeField(list, "Hipóteses", (data.hypotheses || []).map((item) => `${item.statement} (a validar: ${item.missing_validation})`));
    } else if (mode === "relationship_risk") {
      appendModeField(list, "Nível de risco", data.risk_level);
      appendModeField(list, "Força do sinal de churn", data.churn_signal_strength);
      appendModeField(list, "Justificativa", data.rationale);
      appendModeField(list, "Sinais positivos", (data.positive_signals || []).map((item) => item.statement));
      appendModeField(list, "Sinais negativos", (data.negative_signals || []).map((item) => item.statement));
      appendModeField(list, "Fatores que reduzem a confiança", data.factors_reducing_confidence);
    }
    card.append(list);
    appendModeEvidence(card, data.evidence || []);
    if (data.uncertainties?.length) appendModeField(list, "Incertezas", data.uncertainties);
    return card;
  }

  function formatTranscriptTime(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "";
    const totalSeconds = Math.floor(milliseconds / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function speakerInitials(name) {
    return name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0])
      .join("")
      .toLocaleUpperCase("pt-BR") || "?";
  }

  function renderAuditTranscript(container, segments) {
    if (!container) return;
    container.replaceChildren();
    segments.forEach((segment) => {
      const speaker = segment.speaker || "Falante não identificado";
      const row = document.createElement("article");
      row.id = `segment-${segment.id}`;
      row.className = `audit-message ${segment.is_totvs ? "audit-message-totvs" : "audit-message-client"}`;

      const avatar = document.createElement("span");
      avatar.className = "audit-message-avatar";
      avatar.textContent = speakerInitials(speaker);
      avatar.setAttribute("aria-hidden", "true");

      const bubble = document.createElement("div");
      bubble.className = "audit-message-bubble";
      const heading = document.createElement("header");
      const author = document.createElement("strong");
      author.textContent = speaker;
      const metadata = document.createElement("small");
      const timestamp = formatTranscriptTime(segment.started_at_ms);
      metadata.textContent = [`Trecho ${segment.position}`, timestamp, segment.id].filter(Boolean).join(" · ");
      heading.append(author, metadata);

      const text = document.createElement("p");
      text.textContent = segment.text;
      bubble.append(heading, text);
      row.append(avatar, bubble);
      container.append(row);
    });
    if (!container.children.length) container.textContent = "Transcrição indisponível para esta análise.";
  }
  function renderModeAuditSources(payload) {
    const manufacturingAudit = document.querySelector("#audit-manufacturing-brief");
    if (manufacturingAudit) {
      manufacturingAudit.textContent = "Esta versão utilizou modos especializados; as conclusões e citações aparecem nos cards acima.";
    }
    const context = document.querySelector("#audit-context");
    if (context) context.textContent = JSON.stringify(payload.audit_context || {}, null, 2);
    renderAuditTranscript(document.querySelector("#audit-transcript"), payload.segments || []);
    const actions = document.querySelector("#audit-actions");
    actions?.replaceChildren();
    (payload.actions || []).forEach((action) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = `${action.title} · ${action.status}`;
      const details = document.createElement("p");
      details.textContent = action.rationale || "Sem justificativa registrada.";
      card.append(title, details);
      actions?.append(card);
    });
    if (actions && !actions.children.length) actions.textContent = "Nenhuma sugestão foi enviada ao Kanban.";
    const executions = document.querySelector("#audit-executions");
    executions?.replaceChildren();
    (payload.executions || []).forEach((execution) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = `Tentativa ${execution.attempt} · ${execution.status}`;
      const details = document.createElement("p");
      details.textContent = `${execution.model} · ${execution.latency_ms || 0} ms · schema ${execution.schema_sha256 || "—"}`;
      card.append(title, details);
      executions?.append(card);
    });
    if (executions && !executions.children.length) executions.textContent = "Nenhuma tentativa registrada.";
  }

  function renderModesResult(payload) {
    const result = payload.result || {};
    const routing = result.routing || {};
    const synthesis = result.synthesis || {};
    const summary = synthesis.executive_summary || {};
    document.querySelectorAll("[data-legacy-analysis-section]").forEach((item) => { item.hidden = true; });
    const meta = document.querySelector("#modes-analysis-meta");
    const routingBox = document.querySelector("#modes-routing");
    const modeSection = document.querySelector("#mode-results-section");
    if (meta) meta.hidden = false;
    if (routingBox) routingBox.hidden = false;
    if (modeSection) modeSection.hidden = false;
    const modeValues = {
      source: routing.source === "manual" ? "Selecionado pelo analista" : "Sugerido pela IA",
      meeting_type: meetingTypeLabels[routing.meeting_type] || routing.meeting_type,
      selected_modes: (routing.selected_modes || payload.selected_modes || []).map((mode) => modeLabels[mode] || mode).join(", "),
    };
    document.querySelectorAll("[data-mode-value]").forEach((element) => {
      element.textContent = modeValues[element.dataset.modeValue] || "Não informado";
    });
    const reason = document.querySelector("#modes-routing-reason");
    if (reason) reason.textContent = routing.reason || "Combinação definida diretamente pelo analista.";
    const badge = document.querySelector("#modes-routing-badge");
    if (badge) badge.textContent = routing.source === "manual" ? "Selecionado pelo analista" : "Sugerido pela IA";
    const summaryValues = {
      what_happened: summary.what_happened,
      why_it_matters: summary.why_it_matters,
      next_step: summary.next_step,
    };
    document.querySelectorAll("[data-analysis-value]").forEach((element) => {
      if (summaryValues[element.dataset.analysisValue]) element.textContent = summaryValues[element.dataset.analysisValue];
    });
    const resultsBox = document.querySelector("#mode-results");
    resultsBox?.replaceChildren();
    Object.entries(result.mode_results || {}).forEach(([mode, data]) => {
      resultsBox?.append(renderModeCard(mode, data || {}));
    });
    const uncertaintyList = document.querySelector("#analysis-uncertainties");
    uncertaintyList?.replaceChildren();
    (synthesis.uncertainties || []).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      uncertaintyList?.append(item);
    });
    if (uncertaintyList && !uncertaintyList.children.length) {
      const item = document.createElement("li");
      item.textContent = "Nenhuma incerteza adicional registrada.";
      uncertaintyList.append(item);
    }
    const traceability = { ...(payload.traceability || {}), ...(result.traceability || {}) };
    const traceabilityList = document.querySelector("#analysis-traceability");
    traceabilityList?.replaceChildren();
    [
      ["Modelo", traceability.model],
      ["Bundle", payload.mode_bundle_version || result.schema_version],
      ["Perfil da análise", payload.analysis_profile_hash],
      ["Hash da transcrição", traceability.transcript_hash],
      ["Tokens de entrada", traceability.input_tokens],
      ["Tokens de saída", traceability.output_tokens],
      ["Latência", traceability.latency_ms ? `${traceability.latency_ms} ms` : null],
    ].forEach(([label, value]) => appendModeField(traceabilityList, label, value));
    renderModeAuditSources(payload);
    processing.hidden = true;
    resultBox.hidden = false;
  }

  function renderResult(payload) {
    const result = payload.result || {};
    if (result.schema_version === "meetmind-modes-v1") {
      renderModesResult(payload);
      return;
    }
    document.querySelectorAll("[data-legacy-analysis-section]").forEach((item) => { item.hidden = false; });
    ["#modes-analysis-meta", "#modes-routing", "#mode-results-section"].forEach((selector) => {
      const element = document.querySelector(selector);
      if (element) element.hidden = true;
    });
    const summary = result.executive_summary || {};
    const labels = {
      positive: "positivo",
      neutral: "neutro",
      negative: "negativo",
      mixed: "misto",
      low: "baixo",
      medium: "médio",
      high: "alto",
      critical: "crítico",
      immediate: "imediata",
      next_7_days: "próximos 7 dias",
      next_30_days: "próximos 30 dias",
      monitor: "monitorar",
      healthy: "saudável",
      attention: "atenção",
      at_risk: "em risco",
      insufficient_evidence: "evidência insuficiente",
      support_incident: "suporte/incidente",
      implementation: "implantação",
      relationship_success: "relacionamento/sucesso",
      commercial_expansion: "expansão comercial",
      adoption_training: "adoção/treinamento",
      renewal_contract: "renovação/contrato",
      discovery: "descoberta",
      unknown: "não definida",
    };
    const classification = result.meeting_classification || {};
    const account = result.account_context || {};
    const health = result.relationship_health || {};
    const values = {
      sentiment: labels[result.overall_sentiment] || result.overall_sentiment,
      risk: labels[result.risk_level] || result.risk_level,
      products: (result.products_mentioned || []).join(", ") || "nenhum confirmado",
      what_happened: summary.what_happened,
      why_it_matters: summary.why_it_matters,
      next_step: summary.next_step,
      sector: classification.sector || account.sector,
      meeting_type: labels[classification.meeting_type] || classification.meeting_type,
      relationship_health: labels[health.status] || health.status,
      relationship_rationale: health.rationale,
      contextual_priority: labels[result.contextual_priority] || result.contextual_priority,
    };
    document.querySelectorAll("[data-analysis-value]").forEach((element) => {
      element.textContent = String(values[element.dataset.analysisValue] || "Não informado");
    });

    const retentionBox = document.querySelector("#retention-signals");
    retentionBox?.replaceChildren();
    (result.retention_signals || []).forEach((signal) => {
      const card = document.createElement("article");
      card.className = `sinal-retencao sinal-${signal.signal_type || "attention"}`;
      const title = document.createElement("strong");
      title.textContent = signal.label || "Sinal";
      const description = document.createElement("p");
      description.textContent = signal.description || "Sem descrição.";
      const meta = document.createElement("small");
      const evidenceIds = (signal.evidence || []).map((item) => item.segment_id).join(", ");
      meta.textContent = `Confiança ${Math.round(Number(signal.confidence || 0) * 100)}% · segmentos ${evidenceIds || "não informados"}`;
      card.append(title, description, meta);
      retentionBox?.append(card);
    });
    if (retentionBox && !retentionBox.children.length) {
      retentionBox.textContent = "Nenhum sinal adicional foi confirmado na conversa.";
    }

    const accountList = document.querySelector("#account-context");
    accountList?.replaceChildren();
    [
      ["Setor", account.sector],
      ["Subsetor", account.subsector],
      ["Região / UF", [account.region, account.state].filter(Boolean).join(" / ")],
      ["Faixa de faturamento", account.revenue_band],
      [
        "Completude",
        Number.isFinite(Number(account.completeness))
          ? `${Math.round(Number(account.completeness) * 100)}%`
          : null,
      ],
    ].forEach(([label, value]) => {
      if (!value) return;
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = String(value);
      accountList?.append(term, detail);
    });

    const contextBox = document.querySelector("#context-application");
    contextBox?.replaceChildren();
    (result.context_application || []).forEach((item) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = `${item.factor || "contexto"} · ${item.value || ""}`;
      const influence = document.createElement("p");
      influence.textContent = item.influence || "Sem influência registrada.";
      const limitation = document.createElement("small");
      limitation.textContent = item.limitation || "Não substitui evidência da reunião.";
      card.append(title, influence, limitation);
      contextBox?.append(card);
    });
    if (contextBox && !contextBox.children.length) {
      contextBox.textContent = "O contexto não alterou a prioridade desta leitura.";
    }

    const findingsBox = document.querySelector("#analysis-findings");
    findingsBox?.replaceChildren();
    (result.findings || []).forEach((finding) => {
      const card = document.createElement("article");
      card.className = "achado-card";

      const header = document.createElement("header");
      const title = document.createElement("h4");
      title.textContent = finding.title || "Achado";
      const confidence = document.createElement("span");
      confidence.className = "etiqueta";
      confidence.textContent = `Confiança ${Math.round(Number(finding.confidence || 0) * 100)}%`;
      header.append(title, confidence);
      card.append(header);

      [
        ["Observado", finding.observation],
        ["Interpretação", finding.interpretation],
        ["Impacto", finding.business_impact],
        ["Recomendação", finding.recommendation],
      ].forEach(([label, value]) => {
        const paragraph = document.createElement("p");
        const strong = document.createElement("strong");
        strong.textContent = `${label}: `;
        paragraph.append(strong, document.createTextNode(String(value || "Não informado")));
        card.append(paragraph);
      });

      const trigger = document.createElement("div");
      trigger.className = "trigger-explicacao";
      const triggerTitle = document.createElement("strong");
      triggerTitle.textContent = `Trigger · ${finding.trigger?.label || "não informado"}`;
      const triggerReason = document.createElement("p");
      triggerReason.textContent = finding.trigger?.reason || "Justificativa não informada.";
      trigger.append(triggerTitle, triggerReason);
      card.append(trigger);

      const evidenceList = document.createElement("div");
      evidenceList.className = "evidencias-lista";
      (finding.evidence || []).forEach((evidence) => {
        const quote = document.createElement("blockquote");
        const text = document.createElement("p");
        text.textContent = `“${evidence.quote || ""}”`;
        const source = document.createElement("footer");
        const speaker = evidence.speaker ? `${evidence.speaker} · ` : "";
        const verification =
          evidence.verification_method === "deterministic_alignment"
            ? " · trecho realinhado pelo backend"
            : " · trecho literal verificado";
        source.textContent = `${speaker}segmento ${evidence.segment_id}${verification} · ${evidence.relevance}`;
        quote.append(text, source);
        evidenceList.append(quote);
      });
      card.append(evidenceList);
      findingsBox?.append(card);
    });
    if (findingsBox && !findingsBox.children.length) {
      findingsBox.textContent = "Nenhum achado rastreável foi retornado.";
    }

    const triggerBox = document.querySelector("#local-trigger-events");
    triggerBox?.replaceChildren();
    (payload.local_metrics?.trigger_events || []).slice(0, 8).forEach((event) => {
      const card = document.createElement("article");
      card.className = "trigger-local-card";
      const title = document.createElement("strong");
      title.textContent = `${event.term} · ${event.category}`;
      const impact = document.createElement("span");
      impact.className = "etiqueta";
      impact.textContent = `impacto ${Number(event.score_impact || 0).toFixed(1)}`;
      const excerpt = document.createElement("p");
      excerpt.textContent = event.evidence?.[0]?.excerpt || "Sem trecho disponível.";
      const source = document.createElement("small");
      source.textContent = event.evidence?.[0]?.segment_id
        ? `Origem: segmento ${event.evidence[0].segment_id}`
        : "Origem não identificada.";
      card.append(title, impact, excerpt, source);
      triggerBox?.append(card);
    });
    if (triggerBox && !triggerBox.children.length) {
      triggerBox.textContent = "Nenhum trigger determinístico identificado.";
    }

    const actionList = document.querySelector("#analysis-actions");
    actionList?.replaceChildren();
    (result.action_items || []).forEach((action) => {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = action.action || "Ação";
      const detail = document.createElement("p");
      const owner = action.owner ? ` · Responsável: ${action.owner}` : "";
      detail.textContent = `${action.rationale || ""} · Urgência: ${
        labels[action.urgency] || action.urgency || "não informada"
      }${owner}`;
      item.append(title, detail);
      actionList?.append(item);
    });
    if (actionList && !actionList.children.length) {
      const item = document.createElement("li");
      item.textContent = "Nenhuma ação adicional registrada.";
      actionList.append(item);
    }

    const uncertaintyList = document.querySelector("#analysis-uncertainties");
    uncertaintyList?.replaceChildren();
    (result.uncertainties || []).forEach((uncertainty) => {
      const item = document.createElement("li");
      item.textContent = uncertainty;
      uncertaintyList?.append(item);
    });
    if (uncertaintyList && !uncertaintyList.children.length) {
      const item = document.createElement("li");
      item.textContent = "Nenhuma incerteza adicional registrada.";
      uncertaintyList.append(item);
    }

    const traceability = { ...(payload.traceability || {}), ...(result.traceability || {}) };
    const traceabilityList = document.querySelector("#analysis-traceability");
    traceabilityList?.replaceChildren();
    [
      ["Modelo", traceability.model],
      ["Política", traceability.policy_version || traceability.prompt_version],
      ["Versão do score", traceability.score_version],
      ["Evidências verificadas", traceability.evidence_count],
      ["Trechos recebidos já exatos", traceability.exact_evidence_count],
      ["Trechos realinhados pelo backend", traceability.realigned_evidence_count],
      ["Trechos reassociados por citação literal única", traceability.reassigned_evidence_segment_count],
      ["Referências sem suporte descartadas", traceability.discarded_reference_count],
      ["Reparos de formato", traceability.format_repair_count],
      ["Tempo de processamento", traceability.latency_ms ? `${traceability.latency_ms} ms` : null],
      ["Tokens de entrada", traceability.input_tokens],
      ["Tokens de saída", traceability.output_tokens],
      ["Hash da transcrição", traceability.transcript_hash],
      ["Versão do contexto", traceability.context_version],
      [
        "Playbook",
        [traceability.playbook_id, traceability.playbook_version].filter(Boolean).join(" · "),
      ],
      ["Origem do setor", traceability.sector_source],
      [
        "Completude do contexto",
        traceability.context_completeness !== undefined
          ? `${Math.round(Number(traceability.context_completeness) * 100)}%`
          : null,
      ],
      ["Hash do contexto", traceability.context_sha256],
    ].forEach(([label, value]) => {
      if (value === null || value === undefined || value === "") return;
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = String(value);
      traceabilityList?.append(term, detail);
    });

    const executionsBox = document.querySelector("#audit-executions");
    const manufacturingAudit = document.querySelector("#audit-manufacturing-brief");
    manufacturingAudit?.replaceChildren();
    const manufacturingBrief = result.manufacturing_brief;
    if (manufacturingBrief && manufacturingAudit) {
      const auditItems = [
        ["O que aconteceu", manufacturingBrief.what_happened, manufacturingBrief.what_happened_evidence],
        ["Objetivo", manufacturingBrief.objective?.rationale, manufacturingBrief.objective?.evidence],
        ["Próximo encontro", manufacturingBrief.follow_up?.rationale, manufacturingBrief.follow_up?.evidence],
        ["Conflito", manufacturingBrief.conflict?.rationale, manufacturingBrief.conflict?.evidence],
        ["Preocupação", manufacturingBrief.concern?.rationale, manufacturingBrief.concern?.evidence],
        ...(manufacturingBrief.customer_requests || []).map((item) => ["Pedido do cliente", item.request, item.evidence]),
      ];
      auditItems.forEach(([label, value, evidence]) => {
        const card = document.createElement("article");
        const title = document.createElement("strong");
        title.textContent = label;
        const detail = document.createElement("p");
        detail.textContent = value || "Sem conclusão registrada.";
        card.append(title, detail);
        (evidence || []).forEach((item) => {
          const quote = document.createElement("blockquote");
          const link = document.createElement("a");
          link.href = `#segment-${item.segment_id}`;
          link.textContent = `“${item.quote}” — ${item.speaker || "falante não identificado"} · ${item.segment_id}`;
          quote.append(link);
          card.append(quote);
        });
        manufacturingAudit.append(card);
      });
    }
    if (manufacturingAudit && !manufacturingAudit.children.length) manufacturingAudit.textContent = "Esta versão da análise não possuía decisões de manufatura estruturadas.";

    executionsBox?.replaceChildren();
    (payload.executions || []).forEach((execution) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = `Tentativa ${execution.attempt} · ${execution.status}`;
      const detail = document.createElement("p");
      const cost = execution.estimated_cost_microunits == null
        ? "custo não calculado"
        : `${execution.estimated_cost_microunits} microunidades ${execution.cost_currency || ""}`;
      detail.textContent = `${execution.model} · ${execution.latency_ms || 0} ms · ${execution.input_tokens || 0}/${execution.output_tokens || 0} tokens · ${cost}`;
      const hashes = document.createElement("small");
      hashes.textContent = `schema ${execution.schema_sha256 || "—"} · resultado ${execution.result_sha256 || "—"}${execution.error_code ? ` · erro ${execution.error_code}` : ""}`;
      card.append(title, detail, hashes);
      executionsBox?.append(card);
    });
    if (executionsBox && !executionsBox.children.length) executionsBox.textContent = "Nenhuma tentativa registrada.";

    const contextBoxFull = document.querySelector("#audit-context");
    if (contextBoxFull) contextBoxFull.textContent = JSON.stringify(payload.audit_context || {}, null, 2);

    const auditActions = document.querySelector("#audit-actions");
    auditActions?.replaceChildren();
    (payload.actions || []).forEach((action) => {
      const card = document.createElement("article");
      const title = document.createElement("strong");
      title.textContent = `${action.title} · ${action.status}`;
      const detail = document.createElement("p");
      detail.textContent = action.rationale || "Sem justificativa registrada.";
      const events = document.createElement("small");
      events.textContent = (action.events || []).map((event) => `${event.type} em ${event.occurred_at}`).join(" · ") || "Sem eventos adicionais.";
      card.append(title, detail, events);
      auditActions?.append(card);
    });
    if (auditActions && !auditActions.children.length) auditActions.textContent = "Nenhuma sugestão foi enviada ao Kanban.";

    renderAuditTranscript(document.querySelector("#audit-transcript"), payload.segments || []);

    processing.hidden = true;
    resultBox.hidden = false;
  }

  async function poll(statusUrl, requestNumber = selectedMeetingRequest) {
    const payload = await apiFetch(statusUrl);
    if (requestNumber !== selectedMeetingRequest) return;
    if (payload.status === "succeeded") {
      renderResult(payload);
      showNotice("Análise concluída.", "success");
      return;
    }
    if (payload.status === "failed") {
      processing.hidden = true;
      showNotice(analysisErrorMessage(payload.error), "error");
      return;
    }
    window.setTimeout(
      () =>
        poll(statusUrl, requestNumber).catch((error) => {
          if (requestNumber === selectedMeetingRequest) showNotice(error.message, "error");
        }),
      2000
    );
  }

  analysisButton?.addEventListener("click", async () => {
    try {
      const requestNumber = ++selectedMeetingRequest;
      resultBox.hidden = true;
      processing.hidden = false;
      const payload = await apiFetch(`/api/v1/meetings/${select.value}/analyses`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (payload.status === "succeeded") {
        await poll(payload.status_url, requestNumber);
      } else {
        showNotice("Análise adicionada à fila.", "info");
        await poll(payload.status_url, requestNumber);
      }
    } catch (error) {
      processing.hidden = true;
      showNotice(error.message, "error");
    }
  });

  document.querySelector("#reanalyze-button")?.addEventListener("click", () => {
    select?.scrollIntoView({ behavior: "smooth", block: "center" });
    showNotice("Confirme a reunião e solicite uma nova análise.", "info");
  });

  loadLatestAnalysis();
}

const analysisHistory = document.querySelector("[data-analysis-history]");
if (analysisHistory) {
  const cards = [...analysisHistory.querySelectorAll("[data-history-card]")];
  const searchInput = analysisHistory.querySelector("[data-history-search]");
  const dateFromInput = analysisHistory.querySelector("[data-history-date-from]");
  const dateToInput = analysisHistory.querySelector("[data-history-date-to]");
  const riskInput = analysisHistory.querySelector("[data-history-risk]");
  const productInput = analysisHistory.querySelector("[data-history-product]");
  const confidenceInput = analysisHistory.querySelector("[data-history-confidence]");
  const sectorInput = analysisHistory.querySelector("[data-history-sector]");
  const meetingTypeInput = analysisHistory.querySelector("[data-history-meeting-type]");
  const healthInput = analysisHistory.querySelector("[data-history-health]");
  const priorityInput = analysisHistory.querySelector("[data-history-priority]");
  const visibleCount = analysisHistory.querySelector("[data-history-visible-count]");
  const emptyState = analysisHistory.querySelector("[data-history-empty]");
  const tabs = [...analysisHistory.querySelectorAll("[data-history-tab]")];
  let activeGroup = "all";

  function matchesConfidence(value, filter) {
    if (!filter) return true;
    if (filter === "high") return value >= 80;
    if (filter === "medium") return value >= 65 && value < 80;
    return value < 65;
  }

  function applyHistoryFilters() {
    const query = searchInput?.value.trim().toLocaleLowerCase("pt-BR") || "";
    const from = dateFromInput?.value || "";
    const to = dateToInput?.value || "";
    const risk = riskInput?.value || "";
    const product = productInput?.value.toLocaleLowerCase("pt-BR") || "";
    const confidence = confidenceInput?.value || "";
    const sector = sectorInput?.value.toLocaleLowerCase("pt-BR") || "";
    const meetingType = meetingTypeInput?.value || "";
    const health = healthInput?.value || "";
    const priority = priorityInput?.value || "";
    let total = 0;

    cards.forEach((card) => {
      const groups = (card.dataset.groups || "").split(" ");
      const date = card.dataset.date || "";
      const visible =
        groups.includes(activeGroup) &&
        (!query || card.textContent.toLocaleLowerCase("pt-BR").includes(query)) &&
        (!from || (date && date >= from)) &&
        (!to || (date && date <= to)) &&
        (!risk || card.dataset.risk === risk) &&
        (!product || (card.dataset.products || "").includes(product)) &&
        (!sector || card.dataset.sector === sector) &&
        (!meetingType || card.dataset.meetingType === meetingType) &&
        (!health || card.dataset.health === health) &&
        (!priority || card.dataset.priority === priority) &&
        matchesConfidence(Number(card.dataset.confidence || 0), confidence);
      card.hidden = !visible;
      if (visible) total += 1;
    });

    if (visibleCount) visibleCount.textContent = String(total);
    if (emptyState) emptyState.hidden = total !== 0;
  }

  [
    searchInput,
    dateFromInput,
    dateToInput,
    riskInput,
    productInput,
    confidenceInput,
    sectorInput,
    meetingTypeInput,
    healthInput,
    priorityInput,
  ]
    .filter(Boolean)
    .forEach((input) => {
      input.addEventListener(input.type === "search" ? "input" : "change", applyHistoryFilters);
    });

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      activeGroup = tab.dataset.historyTab || "all";
      tabs.forEach((item) => {
        const selected = item === tab;
        item.classList.toggle("ativo", selected);
        item.setAttribute("aria-selected", String(selected));
      });
      applyHistoryFilters();
    });
  });

  analysisHistory.querySelectorAll("[data-history-details-button]").forEach((button) => {
    button.addEventListener("click", () => {
      const details = document.getElementById(button.getAttribute("aria-controls"));
      if (!details) return;
      const opening = details.hidden;
      details.hidden = !opening;
      button.setAttribute("aria-expanded", String(opening));
      button.textContent = opening
        ? "Ocultar evidências e rastreabilidade"
        : "Ver evidências e rastreabilidade";
    });
  });
}

document.querySelectorAll("[data-acknowledge]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await apiFetch(`/api/v1/admin/security-events/${button.dataset.acknowledge}/acknowledge`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      button.textContent = "Revisado";
      button.disabled = true;
      showNotice("Grupo de eventos reconhecido.", "success");
    } catch (error) {
      showNotice(error.message, "error");
    }
  });
});
