import {
  loadBriefing, loadDailyBriefing, loadFocus, loadTimeline, markBriefingSeen, sendKanbanRequest, startAction,
} from "./data.js";
import {
  renderBriefing, renderDailyBriefing, renderError, renderFocus, renderTimeline,
} from "./views.js";

const kanbanRouting = Object.freeze({
  performance_analysis: Object.freeze({
    label: "Solicitar análise de lentidão",
    department: "Infraestrutura",
    priority: "high",
  }),
  product_bug: Object.freeze({
    label: "Investigar erro no produto",
    department: "Engenharia de Produto",
    priority: "high",
  }),
  commercial_opportunity: Object.freeze({
    label: "Avaliar oportunidade comercial",
    department: "Comercial",
    priority: "medium",
  }),
  enablement_request: Object.freeze({
    label: "Planejar treinamento",
    department: "Customer Success",
    priority: "medium",
  }),
});

const page = document.querySelector("[data-command-dashboard]");
const regions = Object.fromEntries(
  [...document.querySelectorAll("[data-dashboard-region]")].map((node) => [node.dataset.dashboardRegion, node])
);
const loaders = {
  briefing: [loadBriefing, renderBriefing],
  "daily-briefing": [loadDailyBriefing, renderDailyBriefing],
  timeline: [loadTimeline, renderTimeline],
  focus: [loadFocus, renderFocus],
};
const actions = new Map();
const stageLinks = [...document.querySelectorAll("[data-jump-target]")];
const stages = [...document.querySelectorAll("[data-command-stage]")];
const regionStages = Object.freeze({ briefing: "priorizar", timeline: "entender", focus: "agir" });

function selectSection(name, focusSection = true) {
  const section = stages.find((item) => item.dataset.commandStage === name);
  if (!section) return;
  stages.forEach((item) => { item.hidden = item !== section; });
  stageLinks.forEach((item) => {
    const selected = item.dataset.jumpTarget === name;
    item.classList.toggle("is-active", selected);
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
  });
  try { sessionStorage.setItem("meetmind-dashboard-section", name); } catch (_) { /* optional */ }
  if (focusSection) section.focus({ preventScroll: true });
}

function updateStageCount(region, payload) {
  const stage = regionStages[region];
  if (!stage) return;
  const counter = document.querySelector(`[data-stage-count="${stage}"]`);
  if (!counter) return;
  let count = 0;
  if (region === "briefing") {
    count = (payload.metrics || [])
      .filter((metric) => metric.id !== "completed")
      .reduce((total, metric) => total + Number(metric.value || 0), 0);
  } else {
    count = (payload.items || []).length;
  }
  counter.textContent = count ? String(count) : "";
  counter.hidden = !count;
}

function notify(message, kind = "info") {
  const notice = document.querySelector("#aviso-global");
  if (!notice) return;
  notice.textContent = message;
  notice.className = `aviso aviso-${kind}`;
  notice.hidden = false;
}

async function loadRegion(name) {
  const container = regions[name];
  const config = loaders[name];
  if (!container || !config) return;
  container.setAttribute("aria-busy", "true");
  try {
    const payload = await config[0]();
    if (name === "focus") {
      actions.clear();
      (payload.items || []).forEach((item) => actions.set(item.id, item));
    }
    updateStageCount(name, payload);
    config[1](container, payload);
  } catch (error) {
    renderError(container, error.message, name);
  }
}

function setGreeting() {
  const greeting = document.querySelector("[data-greeting]");
  if (!greeting) return;
  const hour = new Date().getHours();
  greeting.textContent = hour < 12 ? "Bom dia" : hour < 18 ? "Boa tarde" : "Boa noite";
}

const form = document.querySelector("[data-kanban-form]");
const requestType = form?.elements.request_type;
const note = form?.elements.note;
const submit = form?.querySelector('button[type="submit"]');
const sourceBox = document.querySelector("[data-router-source]");
const destinationBox = document.querySelector("[data-router-destination]");
const routerFeedback = document.querySelector("[data-router-feedback]");
let selectedAction = null;

function selectForRouting(action) {
  selectedAction = action;
  form.elements.source_action_id.value = action.id;
  sourceBox.querySelector("strong").textContent = `${action.client} · ${action.title}`;
  requestType.disabled = false;
  note.disabled = false;
  selectSection("acompanhar", false);
  requestAnimationFrame(() => requestType.focus({ preventScroll: true }));
  routerFeedback.textContent = "";
}

function syncDestination() {
  const route = kanbanRouting[requestType?.value];
  destinationBox.querySelector("strong").textContent = route?.department || "Aguardando necessidade";
  submit.disabled = !selectedAction || !route;
}

page?.addEventListener("click", async (event) => {
  const jumpLink = event.target.closest("[data-jump-target]");
  if (jumpLink) {
    selectSection(jumpLink.dataset.jumpTarget);
    return;
  }
  const retry = event.target.closest("[data-retry-region]");
  if (retry) {
    retry.disabled = true;
    await loadRegion(retry.dataset.retryRegion);
    return;
  }
  const markSeen = event.target.closest("[data-mark-briefing-seen]");
  if (markSeen) {
    markSeen.disabled = true;
    try {
      await markBriefingSeen();
      await loadRegion("briefing");
    } catch (error) {
      markSeen.disabled = false;
      notify(error.message, "error");
    }
    return;
  }
  const routeButton = event.target.closest("[data-route-action]");
  if (routeButton && form) {
    const action = actions.get(routeButton.dataset.routeAction);
    if (action) selectForRouting(action);
    return;
  }
  const startButton = event.target.closest("[data-start-action]");
  if (startButton) {
    const action = actions.get(startButton.dataset.startAction);
    if (!action) return;
    startButton.disabled = true;
    startButton.textContent = "Iniciando";
    try {
      await startAction(action);
      notify("Ação iniciada.", "success");
      await Promise.all([loadRegion("briefing"), loadRegion("timeline"), loadRegion("focus")]);
    } catch (error) {
      startButton.disabled = false;
      startButton.textContent = "Iniciar";
      notify(error.message, "error");
    }
  }
});

requestType?.addEventListener("change", syncDestination);
form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const route = kanbanRouting[requestType.value];
  if (!selectedAction || !route) return;
  submit.disabled = true;
  submit.textContent = "Enviando";
  routerFeedback.textContent = "";
  const payload = {
    request_type: requestType.value,
    source_action_id: selectedAction.id,
    note: note.value.trim() || null,
  };
  try {
    const result = await sendKanbanRequest(payload);
    routerFeedback.textContent = `Encaminhamento enviado para ${result.department}. Protocolo ${result.id.slice(0, 8)}.`;
    routerFeedback.className = "router-feedback router-feedback-success";
    requestType.value = "";
    note.value = "";
    syncDestination();
  } catch (error) {
    routerFeedback.textContent = error.message;
    routerFeedback.className = "router-feedback router-feedback-error";
    submit.disabled = false;
  } finally {
    submit.textContent = "Enviar encaminhamento";
  }
});

if (page) {
  setGreeting();
  let initialSection = "priorizar";
  try { initialSection = sessionStorage.getItem("meetmind-dashboard-section") || initialSection; } catch (_) { /* optional */ }
  selectSection(initialSection, false);
  Object.keys(loaders).forEach(loadRegion);
}

document.querySelector(".command-flow")?.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const current = stageLinks.indexOf(document.activeElement);
  let next = current < 0 ? 0 : current;
  if (event.key === "ArrowRight") next = (next + 1) % stageLinks.length;
  if (event.key === "ArrowLeft") next = (next - 1 + stageLinks.length) % stageLinks.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = stageLinks.length - 1;
  stageLinks[next].focus();
  selectSection(stageLinks[next].dataset.jumpTarget, false);
});

export { kanbanRouting };
