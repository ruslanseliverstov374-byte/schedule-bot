// Планёрка — фронтенд без фреймворков.
"use strict";

const state = {
  tab: "today",
  plan: null,
  planDays: 14,
  stats: null,
  tasks: [],
  settings: {},
  subjects: [],
  groups: { universities: [], selected: [] },
  account: { id: 0, name: "", is_owner: false },
  accounts: [],
};

//: Группы по умолчанию для кнопок «только ПГУФКСиТ» / «только КГАСУ».
const DEFAULT_GROUPS = { unifirst: "26282", kgasu: "26ЗК01" };

// ------------------------------------------------------------------ утилиты

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    // Сессия истекла или пароль сменили — отправляем на вход.
    window.location.href = "/planner/login";
    throw new Error("Нужно войти заново");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error("HTTP " + response.status + ": " + text.slice(0, 200));
  }
  return response.json();
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => node.classList.remove("show"), 2600);
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// В ответах ассистента допустимы только <b> и <i> — остальное экранируем.
function chatHtml(text) {
  return escapeHtml(text)
    .replaceAll("&lt;b&gt;", "<b>").replaceAll("&lt;/b&gt;", "</b>")
    .replaceAll("&lt;i&gt;", "<i>").replaceAll("&lt;/i&gt;", "</i>")
    .replaceAll("\n", "<br>");
}

function minutesLabel(minutes) {
  const value = Number(minutes) || 0;
  const hours = Math.floor(value / 60);
  const rest = value % 60;
  if (hours && rest) return hours + " ч " + rest + " мин";
  if (hours) return hours + " ч";
  return rest + " мин";
}

function formatDate(iso) {
  const parts = String(iso || "").split("-");
  if (parts.length !== 3) return iso || "";
  return parts[2] + "." + parts[1];
}

function groupsLabel(groups) {
  if (!groups || !groups.length) return "группа не выбрана";
  return groups.map((item) => item.code).join(" · ");
}

function renderGroupsSummary() {
  const label = groupsLabel(state.groups.selected);
  const summary = document.getElementById("groups-summary");
  if (summary) summary.textContent = label;
  const hint = document.getElementById("groups-hint");
  if (hint) hint.textContent = "Группы: " + label;
}

// ---------------------------------------------------------------- группы

function groupKey(university, code) {
  return university + ":" + code;
}

function selectedSet() {
  return new Set((state.groups.selected || []).map(
    (item) => groupKey(item.university, item.code)));
}

async function loadGroups() {
  const data = await api("/planner/api/groups");
  state.groups = { universities: data.universities || [], selected: data.selected || [] };
  renderGroupsSummary();
  renderGroupPicker();
}

function renderGroupPicker() {
  const container = document.getElementById("group-picker");
  const selected = selectedSet();
  container.innerHTML = "";
  (state.groups.universities || []).forEach((university, index) => {
    const column = document.createElement("div");
    column.className = "group-column";
    const picked = (state.groups.selected || []).filter(
      (item) => item.university === university.university).length;
    column.innerHTML = `
      <h3>${escapeHtml(university.title)}
        <span class="muted small">${university.groups.length} групп</span></h3>
      <div class="hint">${picked ? "выбрано: " + picked : "ничего не выбрано"}</div>
      <input type="search" placeholder="Поиск группы, например 26ЗК"
             data-search="${index}">
      <div class="group-list" data-list="${index}"></div>`;
    container.appendChild(column);
    const list = column.querySelector("[data-list]");
    const search = column.querySelector("[data-search]");
    const fill = (needle) => {
      const query = (needle || "").trim().toLowerCase();
      const matches = university.groups.filter(
        (code) => !query || code.toLowerCase().includes(query));
      list.innerHTML = matches.slice(0, 80).map((code) => {
        const key = groupKey(university.university, code);
        return `<button class="group-chip ${selected.has(key) ? "selected" : ""}"
          data-university="${escapeHtml(university.university)}"
          data-code="${escapeHtml(code)}">${escapeHtml(code)}
          <span class="univ">${escapeHtml(university.university === "kgasu" ? "КГАСУ" : "ПГУФКСиТ")}</span>
        </button>`;
      }).join("") + (matches.length > 80
        ? `<div class="hint">показаны первые 80 — уточни поиск</div>` : "");
      if (!matches.length) list.innerHTML = '<div class="hint">не найдено</div>';
    };
    fill("");
    search.addEventListener("input", () => fill(search.value));
    list.addEventListener("click", (event) => {
      const chip = event.target.closest(".group-chip");
      if (!chip) return;
      const key = groupKey(chip.dataset.university, chip.dataset.code);
      const current = selectedSet();
      if (current.has(key)) {
        current.delete(key);
      } else {
        current.add(key);
      }
      state.groups.selected = [...current].map((item) => {
        const [universityCode, code] = item.split(":");
        return { university: universityCode, code };
      });
      renderGroupPicker();
      renderGroupsSummary();
    });
  });
}

async function saveGroups() {
  const groups = state.groups.selected || [];
  if (!groups.length) {
    toast("Выбери хотя бы одну группу");
    return;
  }
  const result = await post("/planner/api/groups", { groups });
  state.groups.selected = result.selected || groups;
  // Расписание изменилось — пересчитываем план под новые свободные окна.
  await post("/planner/api/plan", {});
  toast("Группы сохранены: " + groupsLabel(state.groups.selected) +
    (result.pairs_today ? " · сегодня пар: " + result.pairs_today.length : " · сегодня пар нет"));
  await loadGroups();
  await loadToday();
  await loadCalendar();
  await loadSettings();
}

function quickGroups(mode) {
  const available = new Set();
  (state.groups.universities || []).forEach((university) => {
    university.groups.forEach((code) => available.add(groupKey(university.university, code)));
  });
  const wanted = [];
  const picked = [];
  if (mode === "both" || mode === "unifirst") picked.push("unifirst");
  if (mode === "both" || mode === "kgasu") picked.push("kgasu");
  picked.forEach((university) => {
    const code = DEFAULT_GROUPS[university];
    if (available.has(groupKey(university, code))) {
      wanted.push({ university, code });
    } else {
      const first = (state.groups.universities || []).find(
        (item) => item.university === university);
      if (first && first.groups.length) {
        wanted.push({ university, code: first.groups[0] });
      }
    }
  });
  state.groups.selected = wanted;
  renderGroupPicker();
  renderGroupsSummary();
}

// ---------------------------------------------------------------- шапка

function renderTop() {
  const block = document.getElementById("topstats");
  const stats = state.stats;
  if (!stats) { block.innerHTML = ""; return; }
  const totals = stats.totals || {};
  block.innerHTML = [
    `<div class="stat"><b>${minutesLabel(totals.planned_today || 0)}</b><span>план на сегодня</span></div>`,
    `<div class="stat"><b>${minutesLabel(totals.done_today || 0)}</b><span>сделано сегодня</span></div>`,
    `<div class="stat"><b>${totals.streak || 0} дн.</b><span>серия занятий</span></div>`,
  ].join("");
}

// --------------------------------------------------------------- сегодня

async function loadToday() {
  const stateData = await api("/planner/api/state");
  state.settings = stateData.settings || {};
  state.subjects = stateData.subjects || [];
  state.account = stateData.account || { id: 0, name: "", is_owner: false };
  state.accounts = stateData.accounts || [];
  renderAccountName();
  if (stateData.groups) {
    state.groups.selected = stateData.groups;
  }
  if (!(state.groups.universities || []).length) {
    await loadGroups();
  }
  renderGroupsSummary();
  document.getElementById("today-label").textContent =
    "сегодня " + new Date().toLocaleDateString("ru-RU",
      { weekday: "long", day: "numeric", month: "long" });

  const warnings = document.getElementById("warnings");
  warnings.innerHTML = (stateData.warnings || [])
    .map((text) => `<div class="warn-chip">⚠️ ${escapeHtml(text)}</div>`).join("");

  const today = stateData.today || { blocks: [], pairs: [], free_minutes: 0 };
  renderBlocks(today);
  renderPairs(today.pairs || []);
  document.getElementById("today-meta").textContent =
    `Свободно ${minutesLabel(today.free_minutes || 0)}, ` +
    `запланировано ${minutesLabel(today.planned_minutes || 0)}, ` +
    `пар: ${(today.pairs || []).length}`;

  const subjectList = document.getElementById("subject-list");
  subjectList.innerHTML = state.subjects
    .map((subject) => `<option value="${escapeHtml(subject)}">`).join("");

  await loadStats();
  renderTop();
}

function renderBlocks(day) {
  const container = document.getElementById("today-blocks");
  const blocks = day.blocks || [];
  if (!blocks.length) {
    container.innerHTML = '<div class="empty">Пока ничего не запланировано — напиши задачу ассистенту.</div>';
    return;
  }
  container.innerHTML = blocks.map((block) => `
    <div class="block ${block.done ? "done" : ""}">
      <div class="time">${escapeHtml(block.start)}<span>${escapeHtml(block.end)}</span></div>
      <div>
        <div class="title">${escapeHtml(block.task_title)}</div>
        <div class="sub">${escapeHtml(block.title)} · ${minutesLabel(block.minutes)}</div>
      </div>
      <div class="actions">
        <button class="icon-btn" title="Отметить" data-subtask="${block.id}"
          data-done="${block.done ? 1 : 0}">${block.done ? "↩️" : "✅"}</button>
        <button class="icon-btn" title="Фокус 25 минут" data-focus="${block.id}"
          data-task="${block.task_id}">⏱</button>
      </div>
    </div>`).join("");
  container.querySelectorAll("[data-subtask]").forEach((button) => {
    button.addEventListener("click", async () => {
      await post(`/planner/api/subtasks/${button.dataset.subtask}/done`,
        { done: button.dataset.done !== "1" });
      toast("Шаг обновлён");
      await loadToday();
    });
  });
  container.querySelectorAll("[data-focus]").forEach((button) => {
    button.addEventListener("click", async () => {
      await post("/planner/api/focus", { minutes: 25, subtask_id: Number(button.dataset.focus),
        task_id: Number(button.dataset.task) });
      toast("Фокус-сессия 25 минут записана");
      await loadStats();
      renderTop();
    });
  });
}

function renderPairs(pairs) {
  const container = document.getElementById("today-pairs");
  if (!pairs.length) {
    container.innerHTML = '<div class="empty">Пар сегодня нет 🎉</div>';
    return;
  }
  container.innerHTML = pairs.map((pair) => `
    <div class="pair">
      <div class="time">${escapeHtml(pair.start)}<br><span class="muted">${escapeHtml(pair.end)}</span></div>
      <div>
        <div class="subject">${escapeHtml(pair.subject)}</div>
        <div class="sub">${escapeHtml(pair.type || "")} ${pair.group ? "· " + escapeHtml(pair.group) : ""}</div>
      </div>
    </div>`).join("");
}

// ------------------------------------------------------------------ задачи

async function loadTasks() {
  const data = await api("/planner/api/tasks");
  state.tasks = data.tasks || [];
  const showDone = document.getElementById("show-done").checked;
  const container = document.getElementById("task-list");
  const tasks = state.tasks.filter((task) => showDone || !task.done);
  if (!tasks.length) {
    container.innerHTML = '<div class="empty">Задач нет. Добавь через чат или форму выше.</div>';
    return;
  }
  container.innerHTML = tasks.map((task) => {
    const risk = task.minutes_total && task.minutes_done < task.minutes_total;
    const badgeRisk = task.overdue ? '<span class="badge overdue">просрочено</span>'
      : (task.done ? '<span class="badge ok">готово</span>' : "");
    const steps = task.steps.map((step) => `
      <div class="step ${step.done ? "done" : ""}">
        <input type="checkbox" data-step="${step.id}" ${step.done ? "checked" : ""}>
        <div class="step-title">${escapeHtml(step.title)}</div>
        <div class="step-plan">${step.planned_date
          ? formatDate(step.planned_date) + " " + escapeHtml(step.planned_start || "")
          : "—"} · ${minutesLabel(step.minutes)}</div>
      </div>`).join("");
    return `
      <div class="task ${task.done ? "done" : ""} ${task.overdue ? "overdue" : ""}">
        <div class="task-head">
          <div class="task-title">${escapeHtml(task.title)}</div>
          ${task.subject ? `<span class="badge subject">${escapeHtml(task.subject)}</span>` : ""}
          <span class="badge kind">${escapeHtml(task.kind)}</span>
          ${badgeRisk}
          ${task.deadline ? `<span class="muted small">срок: ${escapeHtml(task.deadline)}</span>` : ""}
        </div>
        <div class="task-meta">
          <span>${task.steps_done} из ${task.steps_total} шагов</span>
          <span>${minutesLabel(task.minutes_done)} из ${minutesLabel(task.minutes_total)}</span>
          <span>приоритет: ${task.priority === 1 ? "высокий" : task.priority === 3 ? "низкий" : "обычный"}</span>
        </div>
        <div class="progress ${task.progress === 100 ? "good" : ""}">
          <div style="width:${task.progress}%"></div>
        </div>
        <div class="steps">${steps}</div>
        <div class="task-actions">
          <button class="btn small" data-task-done="${task.id}" data-done="${task.done ? 1 : 0}">
            ${task.done ? "Вернуть в работу" : "Задача сделана"}
          </button>
          <button class="btn small danger ghost" data-task-delete="${task.id}">Удалить</button>
        </div>
      </div>`;
  }).join("");

  container.querySelectorAll("[data-step]").forEach((checkbox) => {
    checkbox.addEventListener("change", async () => {
      await post(`/planner/api/subtasks/${checkbox.dataset.step}/done`, { done: checkbox.checked });
      await loadTasks();
      await loadToday();
    });
  });
  container.querySelectorAll("[data-task-done]").forEach((button) => {
    button.addEventListener("click", async () => {
      await post(`/planner/api/tasks/${button.dataset.taskDone}/done`,
        { done: button.dataset.done !== "1" });
      await loadTasks();
      await loadToday();
    });
  });
  container.querySelectorAll("[data-task-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("Удалить задачу и все её шаги?")) return;
      await api(`/planner/api/tasks/${button.dataset.taskDelete}`, { method: "DELETE" });
      toast("Задача удалена");
      await loadTasks();
      await loadToday();
    });
  });
}

// --------------------------------------------------------------- календарь

async function loadCalendar() {
  const data = await api("/planner/api/plan?days=14");
  state.plan = data;
  const container = document.getElementById("calendar");
  container.innerHTML = (data.days || []).map((day) => {
    const weekend = day.reason === "выходной";
    const pairs = (day.pairs || []).map((pair) =>
      `<div class="slot pair">${escapeHtml(pair.start)} ${escapeHtml(pair.subject)}</div>`).join("");
    const blocks = (day.blocks || []).map((block) =>
      `<div class="slot study ${block.done ? "done" : ""}">${escapeHtml(block.start)} ${escapeHtml(block.task_title)}</div>`).join("");
    return `
      <div class="day ${day.is_today ? "today" : ""} ${weekend ? "weekend" : ""}">
        <div class="day-head"><b>${escapeHtml(day.weekday)}</b>
          <span>${formatDate(day.date)}</span></div>
        ${pairs}
        ${blocks}
        <div class="free">свободно ${minutesLabel(day.free_minutes)}</div>
      </div>`;
  }).join("");
}

// -------------------------------------------------------------- статистика

async function loadStats() {
  const data = await api("/planner/api/stats");
  state.stats = data;
  renderTop();
  if (state.tab === "stats") renderStats(data);
}

function renderStats(data) {
  const totals = data.totals || {};
  document.getElementById("stat-cards").innerHTML = [
    ["Серия", totals.streak || 0, "дней подряд с занятиями"],
    ["Сегодня", minutesLabel(totals.planned_today || 0), "запланировано"],
    ["Сделано сегодня", minutesLabel(totals.done_today || 0), "фокус-сессии"],
    ["Всего сделано", minutesLabel(totals.done_total || 0), "за всё время"],
  ].map(([title, value, hint]) =>
    `<div class="stat-card"><b>${value}</b><span>${title}</span>
     <div class="muted small">${hint}</div></div>`).join("");

  document.getElementById("chart-week").innerHTML =
    barChart(data.week || [], { plannedKey: "planned", doneKey: "done", height: 220 });

  document.getElementById("chart-month").innerHTML =
    barChart(data.month || [], { plannedKey: "planned", doneKey: "done", height: 180,
      thin: true });

  document.getElementById("chart-subjects").innerHTML =
    subjectBars(data.subjects || []);
}

function barChart(series, options) {
  if (!series.length) return '<div class="empty">Нет данных</div>';
  const width = 720, height = options.height || 200;
  const padding = { top: 14, right: 10, bottom: 30, left: 42 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const planned = series.map((item) => item[options.plannedKey] || 0);
  const done = series.map((item) => item[options.doneKey] || 0);
  const maxValue = Math.max(60, ...planned, ...done) * 1.15;
  const slot = plotWidth / series.length;
  const barWidth = Math.max(3, slot * (options.thin ? 0.55 : 0.34));
  const scale = (value) => plotHeight - (value / maxValue) * plotHeight;

  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img">`;
  for (let step = 0; step <= 3; step += 1) {
    const value = (maxValue * step) / 3;
    const y = padding.top + scale(value);
    svg += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}"
      stroke="#e7ebf3" stroke-width="1"/>`;
    svg += `<text x="${padding.left - 8}" y="${y + 4}" font-size="10.5" fill="#8b93a6"
      text-anchor="end">${Math.round(value)}</text>`;
  }
  series.forEach((item, index) => {
    const center = padding.left + slot * index + slot / 2;
    const plannedHeight = (planned[index] / maxValue) * plotHeight;
    const doneHeight = (done[index] / maxValue) * plotHeight;
    svg += `<rect x="${center - barWidth - 1}" y="${padding.top + plotHeight - plannedHeight}"
      width="${barWidth}" height="${plannedHeight}" rx="3" fill="#c8d8ff"/>`;
    svg += `<rect x="${center + 1}" y="${padding.top + plotHeight - doneHeight}"
      width="${barWidth}" height="${doneHeight}" rx="3" fill="#4fbf8b"/>`;
    if (!options.thin || index % 5 === 4 || index === series.length - 1) {
      svg += `<text x="${center}" y="${height - 10}" font-size="10.5" fill="#8b93a6"
        text-anchor="middle">${escapeHtml(item.weekday || formatDate(item.date))}</text>`;
    }
  });
  svg += "</svg>";
  return svg + `<div class="legend">
    <span><i style="background:#c8d8ff"></i>план</span>
    <span><i style="background:#4fbf8b"></i>факт</span></div>`;
}

function subjectBars(items) {
  if (!items.length) return '<div class="empty">Нет данных по предметам</div>';
  const max = Math.max(...items.map((item) => Math.max(item.planned, item.done)), 30);
  return items.map((item) => `
    <div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:13px">
        <span>${escapeHtml(item.subject)}</span>
        <span class="muted">${minutesLabel(item.done)} / ${minutesLabel(item.planned)}</span>
      </div>
      <div class="progress"><div style="width:${Math.round(100 * item.planned / max)}%"></div></div>
      <div class="progress good" style="margin-top:3px"><div style="width:${Math.round(100 * item.done / max)}%"></div></div>
    </div>`).join("") + `<div class="legend">
    <span><i style="background:#3f6ef2"></i>план</span>
    <span><i style="background:#1f9d63"></i>сделано</span></div>`;
}

// ------------------------------------------------------------------- чат

function addMessage(text, who) {
  const log = document.getElementById("chat-log");
  const node = document.createElement("div");
  node.className = "msg " + who;
  node.innerHTML = who === "bot" ? chatHtml(text) : escapeHtml(text);
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
}

async function sendChat(text) {
  if (!text.trim()) return;
  addMessage(text, "user");
  const input = document.getElementById("chat-input");
  input.value = "";
  try {
    const result = await post("/planner/api/chat", { text });
    addMessage(result.reply || "…", "bot");
    if (result.changed) {
      await loadTasks();
      await loadToday();
    }
  } catch (error) {
    addMessage("Ошибка: " + error.message, "bot");
  }
}

// -------------------------------------------------------------- настройки

async function loadSettings() {
  const data = await api("/planner/api/state");
  const form = document.getElementById("settings-form");
  Object.entries(data.settings || {}).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (field) field.value = value;
  });
  if (data.groups) state.groups.selected = data.groups;
  if (data.account) state.account = data.account;
  if (data.accounts) state.accounts = data.accounts;
  renderAccountName();
  await loadGroups();
  await loadAccounts();
}

// ---------------------------------------------------------------- аккаунты

function renderAccountName() {
  const node = document.getElementById("account-name");
  if (!node) return;
  node.textContent = state.account.name ? "· " + state.account.name : "";
}

async function loadAccounts() {
  const card = document.getElementById("accounts-card");
  if (!card) return;
  if (!state.account.is_owner) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  const data = await api("/planner/api/accounts");
  state.accounts = data.accounts || [];
  renderAccounts();
}

function renderAccounts() {
  const list = document.getElementById("accounts-list");
  if (!list) return;
  list.innerHTML = (state.accounts || []).map((account) => `
    <div class="account-row">
      <div class="who">${escapeHtml(account.name)}
        <span class="role">${account.is_owner ? "владелец" : "аккаунт"}</span>
      </div>
      <div class="seen">${account.last_seen
        ? "заходил(а): " + escapeHtml(account.last_seen)
        : "ещё не заходил(а)"}</div>
      <div class="actions">
        <button class="btn small" data-reset="${account.id}"
          data-name="${escapeHtml(account.name)}">Новый пароль</button>
        ${account.is_owner ? "" : `<button class="btn small danger ghost"
          data-delete="${account.id}" data-name="${escapeHtml(account.name)}">Удалить</button>`}
      </div>
    </div>`).join("");
  list.querySelectorAll("[data-reset]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("Сменить пароль аккаунту «" + button.dataset.name + "»? Старый перестанет работать."))
        return;
      const result = await post(`/planner/api/accounts/${button.dataset.reset}/password`, {});
      showNewPassword(button.dataset.name, result.password);
      await loadAccounts();
    });
  });
  list.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("Удалить аккаунт «" + button.dataset.name + "» вместе с его задачами?"))
        return;
      await api(`/planner/api/accounts/${button.dataset.delete}`, { method: "DELETE" });
      toast("Аккаунт удалён");
      await loadAccounts();
    });
  });
}

function showNewPassword(name, password) {
  const box = document.getElementById("account-new");
  if (!box) return;
  box.style.display = "block";
  box.innerHTML = `Пароль для «${escapeHtml(name)}»: <code>${escapeHtml(password)}</code>
    <div class="muted small" style="margin-top:6px">
      Показывается один раз — передайте его человеку. Войти: /planner</div>`;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(password).catch(() => {});
  }
}

async function createAccount(event) {
  event.preventDefault();
  const name = document.getElementById("account-name-input").value.trim();
  if (!name) return;
  const password = document.getElementById("account-password-input").value.trim();
  const result = await post("/planner/api/accounts",
    password ? { name, password } : { name });
  document.getElementById("account-name-input").value = "";
  document.getElementById("account-password-input").value = "";
  showNewPassword(result.account.name, result.account.password);
  toast("Аккаунт «" + result.account.name + "» добавлен");
  await loadAccounts();
}

async function saveSettings(event) {
  event.preventDefault();
  const form = document.getElementById("settings-form");
  const payload = {};
  new FormData(form).forEach((value, key) => { payload[key] = value; });
  await post("/planner/api/settings", payload);
  toast("Настройки сохранены");
  await loadToday();
  await loadCalendar();
}

// ------------------------------------------------------------- переключение

function setTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.classList.toggle("active", page.id === "page-" + name);
  });
  if (name === "today") loadToday().catch(handleError);
  if (name === "tasks") loadTasks().catch(handleError);
  if (name === "calendar") loadCalendar().catch(handleError);
  if (name === "stats") loadStats().then(() => renderStats(state.stats)).catch(handleError);
  if (name === "settings") loadSettings().catch(handleError);
}

function handleError(error) {
  console.error(error);
  toast("Ошибка: " + error.message.slice(0, 120));
}

async function replan() {
  try {
    await post("/planner/api/plan", {});
    toast("План пересчитан");
    await loadToday();
    await loadCalendar();
  } catch (error) {
    handleError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("tabs").addEventListener("click", (event) => {
    const button = event.target.closest(".tab");
    if (button) setTab(button.dataset.tab);
  });
  document.getElementById("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendChat(document.getElementById("chat-input").value);
  });
  document.querySelectorAll("#chat-quick .chip").forEach((chip) => {
    chip.addEventListener("click", () => sendChat(chip.dataset.text));
  });
  document.getElementById("quick-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = document.getElementById("quick-title").value.trim();
    if (!title) return;
    await post("/planner/api/tasks", {
      title,
      kind: document.getElementById("quick-kind").value,
      deadline: document.getElementById("quick-deadline").value,
      subject: document.getElementById("quick-subject").value,
    });
    document.getElementById("quick-title").value = "";
    document.getElementById("quick-subject").value = "";
    toast("Задача добавлена, план обновлён");
    await loadTasks();
    await loadToday();
    await loadCalendar();
  });
  document.getElementById("show-done").addEventListener("change", () => loadTasks());
  document.getElementById("replan-btn").addEventListener("click", replan);
  document.getElementById("groups-save").addEventListener("click", () => {
    saveGroups().catch(handleError);
  });
  document.querySelectorAll(".group-quick .btn").forEach((button) => {
    button.addEventListener("click", () => quickGroups(button.dataset.quick));
  });
  document.getElementById("settings-form").addEventListener("submit", saveSettings);
  document.getElementById("ics-btn").addEventListener("click", () => {
    window.location.href = "/planner/api/plan.ics?days=14";
  });
  document.getElementById("remind-btn").addEventListener("click", async () => {
    const result = await post("/planner/api/remind", {});
    toast(result.ok ? "План отправлен в Telegram" : (result.error || "Не получилось"));
  });
  document.getElementById("reset-plan-btn").addEventListener("click", async () => {
    if (!confirm("Снять все запланированные шаги с календаря?")) return;
    await post("/planner/api/plan/reset", {});
    toast("План очищен");
    await loadToday();
    await loadCalendar();
  });
  setTab("today");
});
