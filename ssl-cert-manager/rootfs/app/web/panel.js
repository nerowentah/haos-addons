// Shared app panel: renders the view model from api/state and posts
// toggles/actions back. All URLs are relative so they work behind ingress.
"use strict";

const POLL_MS = 5000;
const THEME_SYNC_MS = 3000;
const MAX_UPLOAD_FILES = 8;
const MAX_UPLOAD_BYTES = 64 * 1024;
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

const SVG_NS = "http://www.w3.org/2000/svg";

function mdi(name, cls) {
  // Inline Material Design Icon from icons.js; unknown names render nothing.
  const key = typeof name === "string" ? name.replace(/^mdi:/, "") : "";
  const path = typeof MDI_PATHS === "object" ? MDI_PATHS[key] : undefined;
  if (!path) return null;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon ${cls || ""}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const shape = document.createElementNS(SVG_NS, "path");
  shape.setAttribute("d", path);
  svg.append(shape);
  return svg;
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

function renderExternal(item, iconName) {
  // An https:// page (e.g. a sign-in) in a new tab; anything else renders nothing.
  if (typeof item.url !== "string" || !item.url.startsWith("https://")) return null;
  const label = item.button || item.label;
  return h(
    "a",
    { class: "btn", href: item.url, target: "_blank", rel: "noopener noreferrer", title: item.url },
    mdi(iconName || "mdi:open-in-new", "btn-icon"),
    label,
  );
}

function renderLinkButton(item, iconName) {
  if (item.url) return renderExternal(item, iconName || item.icon);
  const label = item.button || item.label;
  const glyph = iconName || (label === "Edit" ? "mdi:pencil" : "mdi:open-in-new");
  const button = h("button", { class: "btn", type: "button", title: item.path }, mdi(glyph, "btn-icon"), label);
  button.addEventListener("click", () => navigate(item.path));
  return button;
}

function pickFiles(accept) {
  // The browser's file picker; resolves to [{name, text}] or null when cancelled or refused.
  return new Promise((resolve) => {
    const input = h("input", { type: "file", accept: accept || null, multiple: true, hidden: true });
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(value);
    };
    input.addEventListener("change", async () => {
      const files = Array.from(input.files || []);
      if (files.length === 0) return done(null);
      if (files.length > MAX_UPLOAD_FILES) {
        toast(`Choose at most ${MAX_UPLOAD_FILES} files`, true);
        return done(null);
      }
      const big = files.find((f) => f.size > MAX_UPLOAD_BYTES);
      if (big) {
        toast(`${big.name} is larger than ${MAX_UPLOAD_BYTES / 1024} KB`, true);
        return done(null);
      }
      try {
        done(await Promise.all(files.map(async (f) => ({ name: f.name, text: await f.text() }))));
      } catch (err) {
        toast(err.message, true);
        done(null);
      }
    });
    input.addEventListener("cancel", () => done(null));
    document.body.append(input);
    input.click();
  });
}

function renderAction(item) {
  if (item.path || item.url) return renderLinkButton({ ...item, button: item.label }, item.icon);
  const button = h(
    "button",
    { class: `btn${item.danger ? " danger" : ""}`, type: "button", disabled: item.disabled, title: item.disabled ? item.reason : null },
    mdi(item.icon, "btn-icon"),
    item.label,
  );
  button.addEventListener("click", async () => {
    if (item.confirm && !window.confirm(item.confirm)) return;
    const args = { ...(item.args || {}) };
    if (item.upload) {
      const files = await pickFiles(item.upload);
      if (files === null) return;
      args.files = files;
    }
    if (item.prompt) {
      const text = window.prompt(item.prompt, item.prompt_value || "");
      if (text === null) return;
      args.text = text;
    }
    button.disabled = true;
    pending += 1;
    try {
      const data = await post("api/action", { id: item.id, args });
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

function renderHero(items) {
  return h(
    "div",
    { class: "hero" },
    items.map((item) =>
      h(
        "div",
        { class: "hero-item" },
        h("div", { class: "hero-value" }, item.value, item.unit && item.value !== "-" && h("span", { class: "hero-unit" }, item.unit)),
        h("div", { class: "hero-label" }, item.label),
      ),
    ),
  );
}

function renderProgress(bar) {
  const pct = Math.round(Math.min(Math.max(Number(bar.value) || 0, 0), 1) * 100);
  return h(
    "div",
    { class: "progress" },
    h(
      "div",
      { class: "progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct), "aria-label": bar.label },
      h("div", { class: "progress-fill", style: `width: ${pct}%` }),
    ),
    h("div", { class: "progress-text" }, h("span", {}, bar.label), bar.caption && h("span", { class: "muted" }, bar.caption)),
  );
}

function renderChart(chart) {
  // Inline SVG bars, scaled to the largest value; the last bar is highlighted.
  const bars = chart.bars || [];
  const width = 100;
  const height = 40;
  const gap = 1.5;
  const max = Math.max(...bars.map((b) => Number(b.value) || 0), 0);
  const step = bars.length ? width / bars.length : width;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "chart-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", chart.title || "chart");
  bars.forEach((bar, index) => {
    const value = Number(bar.value) || 0;
    const barHeight = max > 0 ? Math.max((value / max) * (height - 2), value > 0 ? 1 : 0.5) : 0.5;
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", String(index * step + gap / 2));
    rect.setAttribute("y", String(height - barHeight));
    rect.setAttribute("width", String(Math.max(step - gap, 0.5)));
    rect.setAttribute("height", String(barHeight));
    rect.setAttribute("rx", "0.8");
    rect.setAttribute("class", index === bars.length - 1 ? "bar last" : "bar");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${bar.label}: ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${chart.unit ? ` ${chart.unit}` : ""}`;
    rect.append(title);
    svg.append(rect);
  });
  const first = bars[0];
  const last = bars[bars.length - 1];
  return h(
    "div",
    { class: "chart" },
    chart.title && h("div", { class: "chart-title" }, chart.title),
    svg,
    first && last && h("div", { class: "chart-axis" }, h("span", {}, first.label), h("span", {}, last.label)),
    chart.caption && h("div", { class: "chart-caption" }, chart.caption),
  );
}

function storageGet(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch (_err) {
    // Storage blocked: the section just starts closed again after a reload.
  }
}

function renderToggleRows(toggles) {
  return toggles.map((t) =>
    h(
      "div",
      { class: "row" },
      mdi(t.icon, "row-icon"),
      h("div", { class: "row-text" }, h("div", { class: "row-label" }, t.label), t.description && h("div", { class: "row-desc" }, t.description)),
      renderSwitch(t),
    ),
  );
}

function renderToggles(card, toggles) {
  if (!card.toggles_label) return h("div", { class: "rows" }, renderToggleRows(toggles));
  // Collapsible: remember open/closed per card so the 5 s refresh keeps it.
  const key = `panel-open:${card.id || card.title}`;
  const details = h(
    "details",
    { class: "rows toggles", open: storageGet(key) === "1" },
    h("summary", {}, mdi("mdi:cog", "row-icon"), h("span", {}, `${card.toggles_label} (${toggles.length} switch${toggles.length === 1 ? "" : "es"})`)),
    renderToggleRows(toggles),
  );
  details.addEventListener("toggle", () => storageSet(key, details.open ? "1" : "0"));
  return details;
}

function renderCard(card) {
  const stats = card.stats || [];
  const toggles = card.toggles || [];
  const actions = card.actions || [];
  const links = card.links || [];
  const hero = card.hero || [];
  return h(
    "section",
    { class: `card${card.off ? " is-off" : ""}` },
    h(
      "div",
      { class: "card-head" },
      h(
        "div",
        { class: "card-head-main" },
        mdi(card.icon, "card-icon"),
        h("div", {}, h("h3", { class: "card-title" }, card.title), card.subtitle && h("p", { class: "card-sub" }, card.subtitle)),
      ),
      card.badge && h("span", { class: `badge ${card.badge.tone || ""}` }, card.badge.text),
    ),
    h(
      "div",
      { class: "card-body" },
      hero.length > 0 && renderHero(hero),
      card.progress && renderProgress(card.progress),
      card.chart && (card.chart.bars || []).length > 0 && renderChart(card.chart),
      stats.length > 0 &&
        h(
          "div",
          { class: "stats" },
          stats.map((s) =>
            h("div", {}, h("div", { class: "stat-label" }, s.label), h("div", { class: "stat-value" }, s.value, s.unit && s.value !== "-" && h("span", { class: "stat-unit" }, s.unit))),
          ),
        ),
    ),
    toggles.length > 0 && renderToggles(card, toggles),
    links.length > 0 &&
      h(
        "div",
        { class: "rows links" },
        links.map((l) =>
          h(
            "div",
            { class: "row" },
            mdi(l.icon, "row-icon"),
            h("div", { class: "row-text" }, h("div", { class: "row-label" }, l.label), l.description && h("div", { class: "row-desc" }, l.description)),
            (l.path || l.url) && renderLinkButton(l),
          ),
        ),
      ),
    card.notice &&
      h(
        "div",
        { class: `notice ${card.notice.tone || ""}` },
        mdi(card.notice.tone === "ok" || card.notice.tone === "neutral" ? "mdi:information" : "mdi:alert", "notice-icon"),
        h("span", {}, card.notice.text),
      ),
    card.empty && stats.length === 0 && toggles.length === 0 && links.length === 0 && h("div", { class: `empty${card.mono ? " mono" : ""}` }, card.empty),
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
      h("div", {}, section.title && h("h2", { class: "section-title" }, mdi(section.icon, "section-icon"), section.title), h("div", { class: "grid" }, (section.cards || []).map(renderCard))),
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

// Poll only while the panel is visible: a hidden tab or a closed sidebar panel
// costs neither the browser nor the app anything.
syncTheme();
setInterval(() => document.hidden || syncTheme(), THEME_SYNC_MS);
refresh(true);
setInterval(() => document.hidden || refresh(false), POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    syncTheme();
    refresh(true);
  }
});
