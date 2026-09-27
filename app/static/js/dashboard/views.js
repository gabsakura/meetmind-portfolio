const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const date = (value, withTime = false) => {
  if (!value) return "Sem prazo";
  return new Intl.DateTimeFormat("pt-BR", withTime
    ? { dateStyle: "short", timeStyle: "short" }
    : { dateStyle: "short" }).format(new Date(value));
};

export function renderError(container, message, region) {
  container.replaceChildren();
  container.setAttribute("aria-busy", "false");
  const box = element("div", "command-error");
  box.append(element("strong", "", "Não foi possível carregar"), element("p", "", message));
  const retry = element("button", "botao-terciario", "Tentar novamente");
  retry.type = "button";
  retry.dataset.retryRegion = region;
  box.append(retry);
  container.append(box);
}

export function renderBriefing(container, payload) {
  container.replaceChildren();
  const metrics = element("div", "command-metrics");
  (payload.metrics || []).forEach((metric) => {
    const item = element("article", `command-metric command-metric-${metric.tone}`);
    item.append(element("strong", "", String(metric.value)), element("span", "", metric.label));
    metrics.append(item);
  });
  container.append(metrics);
  const updates = payload.updates || [];
  if (updates.length) {
    const section = element("section", "command-personal-updates");
    const heading = element("header", "");
    heading.append(element("strong", "", "Novidades para você"));
    const seen = element("button", "botao-terciario", "Marcar como vistas");
    seen.type = "button";
    seen.dataset.markBriefingSeen = "true";
    heading.append(seen);
    const list = element("div", "command-update-list");
    updates.forEach((update) => {
      const link = element("a", `command-update command-update-${update.tone || "neutral"}`);
      link.href = update.url;
      link.append(
        element("strong", "", update.title),
        element("span", "", update.client),
        element("p", "", update.summary),
      );
      list.append(link);
    });
    section.append(heading, list);
    container.append(section);
  }
  container.setAttribute("aria-busy", "false");
}

const itemDate = (item) => {
  if (item.all_day && /^\d{4}-\d{2}-\d{2}$/.test(item.starts_at)) {
    const [year, month, day] = item.starts_at.split("-").map(Number);
    return new Date(year, month - 1, day);
  }
  return new Date(item.starts_at);
};

const commitmentDay = (item) => {
  const target = itemDate(item);
  const today = new Date();
  const tomorrow = new Date();
  tomorrow.setDate(today.getDate() + 1);
  if (target.toDateString() === today.toDateString()) return "Hoje";
  if (target.toDateString() === tomorrow.toDateString()) return "Amanhã";
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short" }).format(target);
};

export function renderDailyBriefing(container, payload) {
  const items = (payload.items || []).filter((item) => item.starts_at);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const next = items.find((item) => !item.all_day && itemDate(item) >= now)
    || items.find((item) => item.all_day && itemDate(item) >= today);
  const periods = new Map();
  items.forEach((item) => {
    const startsAt = itemDate(item);
    const period = item.all_day ? "Dia inteiro" : startsAt.getHours() < 12 ? "Manhã" : "Tarde";
    const key = `${startsAt.toDateString()}-${period}`;
    if (!periods.has(key)) periods.set(key, { label: `${commitmentDay(item)} · ${period}`, items: [] });
    periods.get(key).items.push(item);
  });
  container.replaceChildren();
  if (!items.length) {
    const empty = element("div", "command-empty compact");
    empty.append(element("strong", "", "Agenda livre"), element("p", "", "Nenhuma reunião ou entrega próxima."));
    container.append(empty);
  } else {
    periods.forEach((group) => {
      const section = element("section", "daily-briefing-period");
      section.append(element("h4", "", group.label));
      const list = element("ol", "daily-briefing-list");
      group.items.forEach((item) => {
        const tone = item.type === "meeting" ? "meeting" : item.critical ? "deadline-high" : "deadline";
        const row = element("li", `daily-briefing-item daily-briefing-${tone}${item.id === next?.id ? " is-next" : ""}`);
        const when = element("div", "daily-briefing-time");
        const clock = item.all_day
          ? "Dia inteiro"
          : new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(itemDate(item));
        when.append(element("strong", "", clock), element("span", "", item.id === next?.id ? "Próximo" : commitmentDay(item)));
        const content = element("div", "daily-briefing-content");
        const title = item.url
          ? element("a", "daily-briefing-link", item.title)
          : element("strong", "", item.title);
        if (item.url) {
          title.href = item.url;
          if (/^https?:\/\//.test(item.url)) {
            title.target = "_blank";
            title.rel = "noopener";
          }
        }
        content.append(title, element("span", "", item.detail));
        row.append(
          when,
          content,
          element(
            "span",
            "daily-briefing-kind",
            item.kind_label || (item.type === "meeting" ? "Reunião" : "Tarefa"),
          ),
        );
        list.append(row);
      });
      section.append(list);
      container.append(section);
    });
  }
  container.setAttribute("aria-busy", "false");
}


export function renderTimeline(container, payload) {
  container.replaceChildren();
  const items = payload.items || [];
  if (!items.length) {
    const empty = element("div", "command-empty");
    empty.append(element("strong", "", "Nenhum evento recente"), element("p", "", "Novas análises e mudanças nas ações aparecerão aqui."));
    container.append(empty);
  } else {
    const list = element("ol", "command-timeline");
    items.forEach((event) => {
      const row = element("li", `command-event command-event-${event.tone}`);
      const link = element("a", "");
      link.href = event.url;
      const header = element("div", "command-event-header");
      header.append(element("strong", "", event.title), element("time", "", date(event.occurred_at, true)));
      link.append(header, element("span", "command-event-client", event.client), element("p", "", event.summary));
      row.append(link);
      list.append(row);
    });
    container.append(list);
  }
  container.setAttribute("aria-busy", "false");
}

export function renderFocus(container, payload) {
  container.replaceChildren();
  const items = payload.items || [];
  if (!items.length) {
    const empty = element("div", "command-empty");
    empty.append(element("strong", "", "Nada exige ação agora"), element("p", "", "O próximo plano ou prazo aparecerá automaticamente."));
    container.append(empty);
  } else {
    const list = element("div", "command-task-list");
    items.forEach((action) => {
      const tone = action.overdue ? "risk" : action.status === "proposed" ? "attention" : "neutral";
      const card = element("article", `command-task command-task-${tone}`);
      const meta = element("div", "command-task-meta");
      meta.append(element("span", `status status-${tone === "risk" ? "risco" : tone === "attention" ? "atencao" : "neutro"}`, action.status_label));
      meta.append(element("span", "", action.client));
      if (action.due_at) meta.append(element("time", "", date(action.due_at)));
      card.append(meta, element("h3", "", action.title), element("p", "command-task-context", action.context));
      if (action.approach) {
        const approach = element("div", "command-approach");
        approach.append(element("span", "", "Abordagem"), element("p", "", action.approach));
        card.append(approach);
      }
      const controls = element("div", "command-task-actions");
      const source = element("a", "botao-terciario link-botao", action.status === "proposed" ? "Revisar evidência" : "Abrir evidência");
      source.href = action.source_url;
      controls.append(source);
      if (action.status === "accepted" && action.can_manage) {
        const start = element("button", "botao-secundario", "Iniciar");
        start.type = "button";
        start.dataset.startAction = action.id;
        controls.append(start);
      }
      if (action.status === "in_progress" && action.can_manage) {
        const complete = element("a", "botao-terciario link-botao", "Concluir");
        complete.href = "/acoes";
        controls.append(complete);
      }
      if (action.can_route) {
        const route = element("button", "botao-primario", "Encaminhar");
        route.type = "button";
        route.dataset.routeAction = action.id;
        controls.append(route);
      }
      card.dataset.action = JSON.stringify(action);
      card.append(controls);
      list.append(card);
    });
    container.append(list);
  }
  container.setAttribute("aria-busy", "false");
}
