const panel = document.querySelector("#meetmind-chat-panel");
const launcher = document.querySelector("[data-chat-open]");
const closeButton = document.querySelector("[data-chat-close]");
const form = document.querySelector("[data-chat-form]");
const input = document.querySelector("#meetmind-chat-question");
const messages = document.querySelector("[data-chat-messages]");
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

if (panel && launcher && form && input && messages) {
  const page = window.location.pathname;
  const history = [];
  let reportContext = null;

  function addMessage(text, kind) {
    const message = document.createElement("article");
    message.className = `meetmind-chat-message ${kind}`;
    message.textContent = text;
    messages.append(message);
    messages.scrollTop = messages.scrollHeight;
    return message;
  }

  function activeMeetingId() {
    return document.querySelector("#ai-meeting-select")?.value
      || document.querySelector("#meeting-select")?.value
      || new URLSearchParams(window.location.search).get("meeting")
      || "";
  }

  function activeAnalysisId() {
    const container = document.querySelector("[data-selected-analysis]");
    const meetingId = activeMeetingId();
    if (container?.dataset.selectedAnalysis && container.dataset.initialMeeting === meetingId) {
      return container.dataset.selectedAnalysis;
    }
    const params = new URLSearchParams(window.location.search);
    return params.get("analysis") && params.get("meeting") === meetingId
      ? params.get("analysis")
      : "";
  }

  async function requestAnswer(question) {
    const response = await fetch("/api/v1/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({
        question,
        page,
        page_context: document.querySelector("#conteudo")?.innerText?.trim().slice(0, 7000) || "",
        meeting_id: activeMeetingId() || null,
        analysis_id: activeAnalysisId() || null,
        history: history.slice(-8),
        report_context: reportContext,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Não foi possível enviar a pergunta.");

    for (let attempt = 0; attempt < 70; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1200));
      const statusResponse = await fetch(payload.status_url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(status.error || "A resposta temporária expirou.");
      if (status.status === "succeeded") return status;
      if (status.status === "failed") throw new Error(status.error || "Não consegui responder agora.");
    }
    throw new Error("A resposta demorou mais que o esperado. Tente novamente.");
  }

  launcher.addEventListener("click", () => {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    input.focus();
  });
  closeButton?.addEventListener("click", () => {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    launcher.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) closeButton?.click();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    addMessage(question, "user");
    input.value = "";
    input.disabled = true;
    form.querySelector("button[type='submit']").disabled = true;
    const pending = addMessage("Vou conferir as informações desta página…", "assistant pending");
    try {
      const result = await requestAnswer(question);
      const answer = result.answer || "Pronto.";
      pending.textContent = answer;
      pending.classList.remove("pending");
      history.push({ role: "user", content: question }, { role: "assistant", content: answer });
      if (history.length > 8) history.splice(0, history.length - 8);
      if (result.report_context) reportContext = result.report_context;
      if (result.attachment) addReportAttachment(pending, result.attachment);
    } catch (error) {
      pending.textContent = error.message;
      pending.classList.remove("pending");
    } finally {
      input.disabled = false;
      form.querySelector("button[type='submit']").disabled = false;
      input.focus();
    }
  });

  function addReportAttachment(message, attachment) {
    if (!attachment || !/^\/api\/v1\/chat\/reports\/[A-Za-z0-9_-]{40,60}$/.test(attachment.url || "")) return;
    const link = document.createElement("a");
    link.className = "meetmind-chat-file";
    link.href = attachment.url;
    link.download = attachment.name || "relatorio_personalizado.pdf";
    link.setAttribute("aria-label", `Baixar ${attachment.name || "relatório PDF"}`);

    const fileIcon = document.createElement("span");
    fileIcon.className = "meetmind-chat-file-icon";
    fileIcon.setAttribute("aria-hidden", "true");
    fileIcon.textContent = "PDF";
    const name = document.createElement("span");
    name.className = "meetmind-chat-file-name";
    name.textContent = attachment.name || "relatorio_personalizado.pdf";
    const svgNamespace = ["http:", "", "www.w3.org", "2000", "svg"].join("/");
    const downloadIcon = document.createElementNS(svgNamespace, "svg");
    downloadIcon.setAttribute("viewBox", "0 0 24 24");
    downloadIcon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNamespace, "path");
    path.setAttribute("d", "M12 3v12m0 0 4-4m-4 4-4-4M5 17v3h14v-3");
    downloadIcon.append(path);

    link.append(fileIcon, name, downloadIcon);
    message.append(link);
    messages.scrollTop = messages.scrollHeight;
  }
}
