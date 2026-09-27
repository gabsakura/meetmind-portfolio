import { loadDailyBriefing } from "./dashboard/data.js";
import { renderDailyBriefing } from "./dashboard/views.js";

const page = document.querySelector("[data-submission-page]");

if (page) {
  const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
  const ALLOWED_EXTENSIONS = new Set(["vtt", "srt", "txt", "csv", "json", "xlsx", "sql"]);
  const TERMINAL_STATUSES = new Set(["completed", "blocked", "failed", "rejected", "expired"]);
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const form = document.querySelector("#submission-form");
  const fileInput = document.querySelector("#submission-file");
  const drop = document.querySelector(".submission-drop");
  const fileName = document.querySelector("[data-file-name]");
  const uploadStatus = document.querySelector("[data-upload-status]");
  const detail = document.querySelector("[data-submission-detail]");
  const liveProgress = document.querySelector("[data-live-progress]");
  let activeSubmissionId = null;
  let trackingGeneration = 0;

  const sourceLabels = { manual_upload: "Envio manual", teams_rsc: "Microsoft Teams" };

  const statusLabels = {
    uploaded: "Recebida",
    screening: "Validando",
    pending_review: "Registro antigo",
    rejected: "Não importada",
    approved: "Registro antigo",
    verifying: "Padronizando",
    needs_review: "Registro antigo",
    analysis_queued: "Analisando com IA",
    completed: "Concluída",
    blocked: "Bloqueada",
    failed: "Interrompida",
    expired: "Expirada",
  };

  const statusMessages = {
    uploaded: ["Reunião recebida", "Preparando o arquivo para a validação automática."],
    screening: [
      "Verificação segura em andamento",
      "Conferindo integridade, conteúdo e estrutura antes de gravar qualquer fala.",
    ],
    verifying: [
      "Padronizando a transcrição",
      "Organizando falantes, falas e tempos no formato que a IA consegue ler.",
    ],
    analysis_queued: [
      "A IA está lendo a reunião",
      "A transcrição segura já entrou na base. O resumo será liberado automaticamente.",
    ],
    completed: [
      "Processamento concluído",
      "A reunião passou pelo protocolo e está disponível no MeetMind.",
    ],
    blocked: [
      "Arquivo bloqueado com segurança",
      "Nenhuma fala desse arquivo entrou na base. Revise o motivo e envie uma versão corrigida.",
    ],
    failed: [
      "Não foi possível concluir",
      "O arquivo não avançou. Você pode tentar novamente sem risco de duplicar a reunião.",
    ],
    rejected: ["Arquivo não importado", "O conteúdo não atendeu às regras do protocolo seguro."],
    expired: ["Envio expirado", "Envie novamente para executar o protocolo automático atual."],
    pending_review: ["Registro de uma versão anterior", "Faça um novo envio para usar o fluxo automático."],
    approved: ["Registro de uma versão anterior", "Este item pertence ao fluxo antigo do produto."],
    needs_review: ["Registro de uma versão anterior", "Faça um novo envio para usar o fluxo automático."],
  };

  const safeErrorMessages = {
    antivirus_blocked: "O antivírus identificou conteúdo inseguro.",
    canonical_protocol_blocked: "A transcrição contém dados ou comandos que não podem seguir para a IA.",
    empty_transcript: "Não foram encontradas falas válidas no arquivo.",
    file_hash_mismatch: "A integridade do arquivo mudou durante o processamento.",
    formula_injection: "O arquivo contém uma fórmula de planilha potencialmente perigosa.",
    ingestion_error: "O processamento automático encontrou um erro inesperado.",
    invalid_meeting_date: "A data informada não é válida.",
    invalid_sector: "Selecione um setor válido para orientar a leitura da IA.",
    invalid_timestamp_order: "Há tempos de fala fora de ordem.",
    non_monotonic_timestamps: "Os trechos da reunião não estão em ordem cronológica.",
    payload_too_large: "O arquivo ultrapassa o limite de 25 MB.",
    pii_detected: "Foram identificados dados pessoais sensíveis no conteúdo.",
    prompt_injection: "O conteúdo tenta alterar indevidamente o comportamento da IA.",
    segment_too_large: "Uma das falas ultrapassa o tamanho permitido.",
    storage_unavailable: "O armazenamento protegido está temporariamente indisponível.",
    timestamp_out_of_range: "Um dos tempos de fala ultrapassa o limite aceito.",
    too_many_segments: "A transcrição possui trechos demais para uma única reunião.",
    unsafe_metadata: "Um dos dados informados no formulário não é seguro.",
  };

  function isTerminal(submission) {
    return TERMINAL_STATUSES.has(submission.status) && !(submission.status === "failed" && submission.retryable);
  }

  function labelFor(submission) {
    if (submission.status === "failed" && submission.retryable) return "Nova tentativa";
    return statusLabels[submission.status] || submission.status;
  }

  function messageFor(submission) {
    if (submission.status === "failed" && submission.retryable) {
      return [
        "Nova tentativa automática agendada",
        "O arquivo permanece protegido e o processamento será retomado sem duplicar a reunião.",
      ];
    }
    return statusMessages[submission.status] || [
      "Processamento automático",
      "O andamento será atualizado sem precisar recarregar a página.",
    ];
  }

  function statusClass(status) {
    if (["blocked", "failed", "rejected"].includes(status)) return "status-risco";
    if (["analysis_queued", "pending_review", "needs_review"].includes(status)) return "status-atencao";
    if (status === "completed") return "status-sucesso";
    return "status-neutro";
  }

  function formatBytes(bytes) {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} MB`;
    }
    return `${(bytes / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} KB`;
  }

  function extensionOf(file) {
    return file?.name.split(".").pop()?.toLowerCase() || "";
  }

  function validateFile(file) {
    if (!file) return "Selecione o arquivo da reunião.";
    if (!ALLOWED_EXTENSIONS.has(extensionOf(file))) {
      return "Formato não aceito. Use VTT, SRT, TXT, CSV, JSON, XLSX ou SQL.";
    }
    if (file.size <= 0) return "O arquivo está vazio.";
    if (file.size > MAX_UPLOAD_BYTES) return "O arquivo ultrapassa o limite de 25 MB.";
    return "";
  }

  function setUploadMessage(message, tone = "neutral") {
    uploadStatus.textContent = message;
    uploadStatus.dataset.tone = tone;
  }

  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const isFormData = options.body instanceof FormData;
    if (options.body && !isFormData) headers.set("Content-Type", "application/json");
    if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
      headers.set("X-CSRFToken", csrfToken);
    }
    const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
    const payload = await response.json().catch(() => ({ error: "Resposta inválida do servidor." }));
    if (!response.ok) throw new Error(payload.error || "Não foi possível concluir.");
    return payload;
  }

  function setFile(file) {
    const error = validateFile(file);
    if (!file) {
      fileName.textContent = "Nenhum arquivo selecionado";
      drop.classList.remove("has-file", "has-error");
      return;
    }
    fileName.textContent = `${file.name} · ${formatBytes(file.size)} · ${extensionOf(file).toUpperCase()}`;
    drop.classList.toggle("has-error", Boolean(error));
    drop.classList.toggle("has-file", !error);
    setUploadMessage(error || "Arquivo pronto para envio.", error ? "error" : "success");
  }

  fileInput?.addEventListener("change", () => setFile(fileInput.files[0]));
  ["dragenter", "dragover"].forEach((eventName) => {
    drop?.addEventListener(eventName, (event) => {
      event.preventDefault();
      drop.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    drop?.addEventListener(eventName, (event) => {
      event.preventDefault();
      drop.classList.remove("dragging");
    });
  });
  drop?.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files[0];
    if (!file || !fileInput) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.files = transfer.files;
    setFile(file);
  });

  function progressState(submission) {
    if (submission.status === "completed") return { current: 5, done: 5, error: false };
    if (["blocked", "rejected", "expired"].includes(submission.status)) {
      return { current: 2, done: 1, error: true };
    }
    if (submission.status === "failed") {
      return { current: submission.analysis_id ? 4 : 2, done: submission.analysis_id ? 3 : 1, error: !submission.retryable };
    }
    if (submission.status === "analysis_queued") return { current: 4, done: 3, error: false };
    if (["verifying", "approved"].includes(submission.status)) return { current: 3, done: 2, error: false };
    return { current: 2, done: 1, error: false };
  }

  function renderLiveProgress(submission) {
    activeSubmissionId = submission.id;
    liveProgress.hidden = false;
    const message = messageFor(submission);
    document.querySelector("[data-live-heading]").textContent = message[0];

    const badge = document.querySelector("[data-live-status]");
    badge.className = `status ${statusClass(submission.status)}`;
    badge.textContent = labelFor(submission);

    const state = progressState(submission);
    document.querySelectorAll("[data-live-step]").forEach((step) => {
      const position = Number(step.dataset.liveStep);
      step.classList.toggle("is-done", position <= state.done);
      step.classList.toggle("is-active", position === state.current && !state.error);
      step.classList.toggle("is-error", position === state.current && state.error);
    });

    const spinner = document.querySelector("[data-live-spinner]");
    spinner.hidden = isTerminal(submission);
    const resultLink = document.querySelector("[data-live-result]");
    resultLink.hidden = !submission.analysis_id;
    if (submission.analysis_id) {
      resultLink.href = `/inteligencia-artificial?analysis=${encodeURIComponent(submission.analysis_id)}`;
      resultLink.textContent = submission.status === "completed" ? "Ver retorno da IA" : "Acompanhar leitura da IA";
    }
  }

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function bindRow(button) {
    if (button.dataset.bound === "true") return;
    button.dataset.bound = "true";
    button.addEventListener("click", () => openDetails(button.dataset.submissionId));
  }

  function upsertSubmissionRow(submission) {
    const list = document.querySelector("[data-mine-list]");
    let row = Array.from(list.querySelectorAll("[data-submission-id]")).find(
      (candidate) => candidate.dataset.submissionId === submission.id,
    );
    if (!row) {
      list.querySelector("[data-mine-empty]")?.remove();
      row = createElement("button", "submission-row");
      row.type = "button";
      row.dataset.submissionId = submission.id;
      const type = createElement("span", "submission-file-type");
      const identity = createElement("span");
      identity.append(createElement("strong"), createElement("small"));
      const state = createElement("span");
      state.dataset.rowStatus = "";
      const time = createElement("time");
      const arrow = createElement("span", "", "›");
      arrow.setAttribute("aria-hidden", "true");
      row.append(type, identity, state, time, arrow);
      list.prepend(row);
      bindRow(row);
    }
    row.querySelector(".submission-file-type").textContent = submission.format.toUpperCase();
    row.querySelector("strong").textContent = submission.proposed_metadata?.title || submission.filename;
    row.querySelector("small").textContent = `${submission.filename} · ${formatBytes(submission.byte_size)}`;
    const state = row.querySelector("[data-row-status]");
    state.className = `status ${statusClass(submission.status)}`;
    state.textContent = labelFor(submission);
    const time = row.querySelector("time");
    time.dateTime = submission.created_at;
    time.textContent = new Date(submission.created_at).toLocaleString("pt-BR", {
      dateStyle: "short",
      timeStyle: "short",
    });
  }

  function appendTag(container, text) {
    container.append(createElement("span", "etiqueta", text));
  }

  function renderTimeline(submission) {
    const container = document.querySelector("[data-detail-timeline]");
    container.replaceChildren();
    const labels = ["Envio", "Segurança", "Formato IA", "Leitura IA", "Resultado"];
    const state = progressState(submission);
    labels.forEach((label, index) => {
      const step = createElement("span", "", label);
      const position = index + 1;
      step.classList.toggle("done", position <= state.done);
      step.classList.toggle("active", position === state.current && !state.error);
      step.classList.toggle("error", position === state.current && state.error);
      container.append(step);
    });
  }

  function appendFact(container, label, value) {
    if (!value) return;
    container.append(createElement("dt", "", label), createElement("dd", "", value));
  }

  function renderDetails(submission, { scroll = false } = {}) {
    detail.hidden = false;
    document.querySelector("[data-detail-title]").textContent =
      submission.proposed_metadata?.title || submission.filename;
    const meta = document.querySelector("[data-detail-meta]");
    meta.replaceChildren();
    appendTag(meta, labelFor(submission));
    appendTag(meta, submission.format.toUpperCase());
    appendTag(meta, formatBytes(submission.byte_size));
    if (submission.protocol_version) appendTag(meta, submission.protocol_version);
    renderTimeline(submission);

    const message = messageFor(submission);
    document.querySelector("[data-request-heading]").textContent = message[0];
    document.querySelector("[data-request-summary]").textContent = message[1];
    document.querySelector("[data-request-message]").dataset.tone = ["blocked", "failed", "rejected"].includes(
      submission.status,
    ) ? "error" : submission.status === "completed" ? "success" : "progress";

    const facts = document.querySelector("[data-request-facts]");
    facts.replaceChildren();
    appendFact(facts, "Origem", sourceLabels[submission.source] || "Envio manual");
    appendFact(facts, "Arquivo", submission.filename);
    appendFact(facts, "Enviado em", new Date(submission.created_at).toLocaleString("pt-BR"));
    appendFact(facts, "Cliente", submission.proposed_metadata?.client_name);
    appendFact(facts, "Setor", submission.proposed_metadata?.sector_label);
    appendFact(facts, "Motivo", safeErrorMessages[submission.error_code]);
    appendFact(facts, "Hash canônico", submission.canonical_sha256);
    appendFact(facts, "Código de acompanhamento", submission.id);

    const retryButton = document.querySelector("[data-retry-submission]");
    retryButton.hidden = !submission.retryable;
    retryButton.dataset.submissionId = submission.retryable ? submission.id : "";

    const canonicalLink = document.querySelector("[data-canonical-link]");
    canonicalLink.hidden = !submission.meeting_id;
    if (submission.meeting_id) {
      canonicalLink.href = `/api/v1/submissions/${encodeURIComponent(submission.id)}/normalized`;
    }
    const resultLink = document.querySelector("[data-result-link]");
    resultLink.hidden = !submission.analysis_id;
    if (submission.analysis_id) {
      resultLink.href = `/inteligencia-artificial?analysis=${encodeURIComponent(submission.analysis_id)}`;
      resultLink.textContent = submission.status === "completed" ? "Ver retorno da IA" : "Acompanhar leitura da IA";
    }
    if (scroll) detail.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function openDetails(id, { scroll = true } = {}) {
    try {
      const payload = await apiFetch(`/api/v1/submissions/${encodeURIComponent(id)}`);
      renderDetails(payload.submission, { scroll });
      if (!isTerminal(payload.submission)) trackSubmission(payload.submission);
      return payload.submission;
    } catch (error) {
      setUploadMessage(error.message, "error");
      return null;
    }
  }

  function pause(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function trackSubmission(initialSubmission) {
    const generation = ++trackingGeneration;
    let submission = initialSubmission;
    let consecutiveFailures = 0;
    renderLiveProgress(submission);
    upsertSubmissionRow(submission);
    for (let attempt = 0; attempt < 160 && !isTerminal(submission); attempt += 1) {
      await pause(1500);
      if (generation !== trackingGeneration) return;
      try {
        const payload = await apiFetch(`/api/v1/submissions/${encodeURIComponent(submission.id)}`);
        submission = payload.submission;
        consecutiveFailures = 0;
        renderLiveProgress(submission);
        upsertSubmissionRow(submission);
        if (!detail.hidden && activeSubmissionId === submission.id) renderDetails(submission);
      } catch (error) {
        consecutiveFailures += 1;
        if (consecutiveFailures >= 3) {
          setUploadMessage("A atualização automática foi pausada. Abra a reunião no histórico para continuar.", "error");
          return;
        }
      }
    }
    if (generation !== trackingGeneration) return;
    if (!isTerminal(submission)) {
      setUploadMessage("A reunião continua sendo processada. Você pode acompanhá-la no histórico abaixo.", "neutral");
      return;
    }
    const successful = submission.status === "completed";
    setUploadMessage(
      successful ? "Reunião concluída. O retorno já está disponível." : "O processamento parou com segurança. Confira o motivo nos detalhes.",
      successful ? "success" : "error",
    );
  }

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selectedFile = fileInput.files[0];
    const fileError = validateFile(selectedFile);
    if (fileError) {
      setUploadMessage(fileError, "error");
      drop.classList.add("has-error");
      return;
    }
    if (!form.checkValidity()) {
      form.reportValidity();
      setUploadMessage("Preencha o título e confirme a autorização para continuar.", "error");
      return;
    }

    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    setUploadMessage("Enviando a reunião para o protocolo seguro…", "neutral");
    try {
      const payload = await apiFetch("/api/v1/submissions", {
        method: "POST",
        body: new FormData(form),
      });
      form.reset();
      setFile(null);
      setUploadMessage("Reunião recebida. Acompanhe o processamento abaixo.", "success");
      trackSubmission(payload.submission);
    } catch (error) {
      setUploadMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  const calendar = document.querySelector("[data-meeting-calendar]");
  if (calendar) {
    loadDailyBriefing()
      .then((payload) => renderDailyBriefing(calendar, {
        ...payload,
        items: (payload.items || []).filter((item) => item.type === "meeting"),
      }))
      .catch(() => {
        calendar.setAttribute("aria-busy", "false");
        calendar.replaceChildren(createElement("p", "estado-vazio", "Não foi possível carregar o calendário."));
      });
  }

  document.querySelector("[data-retry-submission]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const id = button.dataset.submissionId;
    if (!id) return;
    button.disabled = true;
    try {
      const payload = await apiFetch(`/api/v1/submissions/${encodeURIComponent(id)}/retry`, { method: "POST" });
      renderDetails(payload.submission);
      trackSubmission(payload.submission);
    } catch (error) {
      setUploadMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  document.querySelectorAll("[data-submission-id]").forEach(bindRow);
  document.querySelector("[data-detail-close]")?.addEventListener("click", () => {
    detail.hidden = true;
  });
  document.querySelector("[data-live-detail]")?.addEventListener("click", () => {
    if (activeSubmissionId) openDetails(activeSubmissionId);
  });

  const linkedSubmission = new URLSearchParams(window.location.search).get("submission");
  if (linkedSubmission && /^[0-9a-f-]{36}$/i.test(linkedSubmission)) openDetails(linkedSubmission);
}
