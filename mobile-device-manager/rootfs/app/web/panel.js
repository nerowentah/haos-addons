// Shared app panel: renders the view model from api/state and posts
// toggles/actions back. All URLs are relative so they work behind ingress.
"use strict";

const POLL_MS = 5000;
const THEME_SYNC_MS = 3000;
// Home Assistant theme variable -> panel variable.
const THEME_VARS = {
  "--primary-color": "--primary",
  "--primary-text-color": "--text",
  "--secondary-text-color": "--text2",
  "--primary-background-color": "--bg",
  "--card-background-color": "--card",
  "--divider-color": "--divider",
  "--success-color": "--success",
  "--warning-color": "--warning",
  "--error-color": "--error",
  "--switch-checked-color": "--switch-on",
  "--switch-unchecked-track-color": "--switch-off-track",
  "--switch-unchecked-button-color": "--switch-off-knob",
  "--ha-card-border-radius": "--radius",
  "--ha-font-family-body": "--font",
};

let lastJson = "";
let pending = 0;
let toastTimer = 0;

function syncTheme() {
  // The ingress page is same-origin with the HA frontend, so the live theme
  // (including dark mode and custom themes) can be read from the parent.
  try {
    if (window.parent === window) return;
    const doc = window.parent.document;
    const host = doc.querySelector("home-assistant") || doc.documentElement;
    const css = window.parent.getComputedStyle(host);
    const root = document.documentElement;
    let applied = false;
    for (const [haVar, ours] of Object.entries(THEME_VARS)) {
      const value = css.getPropertyValue(haVar).trim();
      if (value) {
        root.style.setProperty(ours, value);
        applied = true;
      }
    }
    if (applied) root.dataset.haTheme = "1";
  } catch (_err) {
    // Not embedded or not same-origin: keep the built-in light/dark defaults.
  }
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function toast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3000);
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = {};
  try {
    data = await response.json();
  } catch (_err) {
    // ignore
  }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function renderSwitch(item) {
  const button = h("button", {
    class: "switch",
    type: "button",
    role: "switch",
    "aria-checked": String(Boolean(item.value)),
    "aria-label": item.label,
    disabled: item.disabled,
  });
  button.addEventListener("click", async () => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(next));
    button.classList.add("pending");
    button.disabled = true;
    pending += 1;
    try {
      await post("api/toggle", { id: item.id, value: next });
      toast(`${item.label}: ${next ? "on" : "off"}`);
    } catch (err) {
      button.setAttribute("aria-checked", String(!next));
      toast(err.message, true);
    } finally {
      pending -= 1;
      button.classList.remove("pending");
      button.disabled = Boolean(item.disabled);
      refresh(true);
    }
  });
  return button;
}

function navigate(path) {
  // Open a Home Assistant page (an automation, a device...) in the main frontend.
  if (typeof path !== "string" || !path.startsWith("/config/")) return;
  try {
    const parent = window.parent;
    if (parent !== window) {
      parent.history.pushState(null, "", path);
      parent.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      return;
    }
  } catch (_err) {
    // Not same-origin: fall through to a full navigation.
  }
  window.top.location.href = path;
}

function renderLinkButton(item) {
  const button = h("button", { class: "btn", type: "button", title: item.path }, item.button || item.label);
  button.addEventListener("click", () => navigate(item.path));
  return button;
}

function renderAction(item) {
  if (item.path) return renderLinkButton({ ...item, button: item.label });
  const button = h(
    "button",
    { class: `btn${item.danger ? " danger" : ""}`, type: "button", disabled: item.disabled, title: item.disabled ? item.reason : null },
    item.label,
  );
  button.addEventListener("click", async () => {
    if (item.confirm && !window.confirm(item.confirm)) return;
    button.disabled = true;
    pending += 1;
    try {
      const data = await post("api/action", { id: item.id, args: item.args || {} });
      toast(data.message || `${item.label}: started`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      pending -= 1;
      button.disabled = Boolean(item.disabled);
      refresh(true);
    }
  });
  return button;
}

function renderCard(card) {
  const stats = card.stats || [];
  const toggles = card.toggles || [];
  const actions = card.actions || [];
  const links = card.links || [];
  return h(
    "section",
    { class: `card${card.off ? " is-off" : ""}` },
    h(
      "div",
      { class: "card-head" },
      h("div", {}, h("h3", { class: "card-title" }, card.title), card.subtitle && h("p", { class: "card-sub" }, card.subtitle)),
      card.badge && h("span", { class: `badge ${card.badge.tone || ""}` }, card.badge.text),
    ),
    h(
      "div",
      { class: "card-body" },
      stats.length > 0 &&
        h(
          "div",
          { class: "stats" },
          stats.map((s) =>
            h("div", {}, h("div", { class: "stat-label" }, s.label), h("div", { class: "stat-value" }, s.value, s.unit && s.value !== "-" && h("span", { class: "stat-unit" }, s.unit))),
          ),
        ),
    ),
    toggles.length > 0 &&
      h(
        "div",
        { class: "rows" },
        toggles.map((t) =>
          h(
            "div",
            { class: "row" },
            h("div", { class: "row-text" }, h("div", { class: "row-label" }, t.label), t.description && h("div", { class: "row-desc" }, t.description)),
            renderSwitch(t),
          ),
        ),
      ),
    links.length > 0 &&
      h(
        "div",
        { class: "rows links" },
        links.map((l) =>
          h(
            "div",
            { class: "row" },
            h("div", { class: "row-text" }, h("div", { class: "row-label" }, l.label), l.description && h("div", { class: "row-desc" }, l.description)),
            renderLinkButton(l),
          ),
        ),
      ),
    card.notice && h("div", { class: `notice ${card.notice.tone || ""}` }, card.notice.text),
    card.empty && stats.length === 0 && toggles.length === 0 && links.length === 0 && h("div", { class: "empty" }, card.empty),
    actions.length > 0 && h("div", { class: "actions" }, actions.map(renderAction)),
  );
}

function render(state) {
  document.title = state.title || "Panel";
  document.getElementById("title").textContent = state.title || "";
  document.getElementById("subtitle").textContent = state.subtitle || "";
  const container = document.getElementById("sections");
  container.replaceChildren(
    ...(state.sections || []).map((section) =>
      h("div", {}, section.title && h("h2", { class: "section-title" }, section.title), h("div", { class: "grid" }, (section.cards || []).map(renderCard))),
    ),
  );
}

async function refresh(force) {
  if (pending > 0 && !force) return;
  try {
    const response = await fetch("api/state", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    if (text !== lastJson && pending === 0) {
      lastJson = text;
      render(JSON.parse(text));
    }
  } catch (err) {
    document.getElementById("subtitle").textContent = `Cannot reach the app (${err.message}); retrying…`;
  }
}

syncTheme();
setInterval(syncTheme, THEME_SYNC_MS);
refresh(true);
setInterval(() => refresh(false), POLL_MS);
