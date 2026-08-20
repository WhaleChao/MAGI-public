(function () {
  "use strict";

  const STORAGE_KEY = "magi.ui.theme.v1";
  const THEMES = Object.freeze({
    cyber: { icon: "☾", label: "夜", next: "日" },
    forest: { icon: "☀", label: "日", next: "夜" },
  });

  function normalize(value) {
    if (value === "forest" || value === "light") return "forest";
    return "cyber";
  }

  function readStored() {
    try {
      const direct = localStorage.getItem(STORAGE_KEY);
      if (direct) return normalize(direct);
      const legacy =
        localStorage.getItem("magi.osc.theme") ||
        localStorage.getItem("magi.golem.theme");
      if (legacy) return normalize(legacy);
    } catch (_) {
      // Storage can be unavailable in hardened/private browser contexts.
    }
    return "cyber";
  }

  function updateButtons(theme) {
    const spec = THEMES[theme];
    document
      .querySelectorAll("#themeToggleBtn, [data-magi-theme-toggle]")
      .forEach((button) => {
        button.classList.add("magi-theme-switch");
        button.dataset.magiThemeToggle = "1";
        button.innerHTML =
          '<span aria-hidden="true">' +
          spec.icon +
          '</span><span class="magi-theme-switch__label">' +
          spec.label +
          "</span>";
        button.setAttribute("aria-label", "目前為" + spec.label + "，切換為" + spec.next);
        button.title = "切換為" + spec.next;
      });
  }

  function apply(value, persist) {
    const theme = normalize(value);
    document.documentElement.dataset.magiTheme = theme;
    if (document.body) {
      document.body.classList.toggle("theme-dark", theme === "cyber");
      document.body.dataset.magiTheme = theme;
    }
    if (persist !== false) {
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch (_) {}
    }
    if (document.readyState !== "loading") updateButtons(theme);
    window.dispatchEvent(
      new CustomEvent("magi-theme-change", { detail: { theme: theme } })
    );
    return theme;
  }

  function current() {
    return normalize(document.documentElement.dataset.magiTheme || readStored());
  }

  function toggle() {
    return apply(current() === "cyber" ? "forest" : "cyber", true);
  }

  function ensureControl() {
    let buttons = document.querySelectorAll(
      "#themeToggleBtn, [data-magi-theme-toggle]"
    );
    if (!buttons.length) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.magiThemeToggle = "1";
      button.className = "magi-theme-switch magi-theme-switch--floating";
      document.body.appendChild(button);
      buttons = [button];
    }
    buttons.forEach((button) => {
      if (button.dataset.magiThemeBound === "1") return;
      button.dataset.magiThemeBound = "1";
      button.addEventListener("click", toggle);
    });
    updateButtons(current());
  }

  window.MAGITheme = Object.freeze({
    apply: apply,
    current: current,
    toggle: toggle,
    themes: Object.keys(THEMES),
  });

  apply(readStored(), false);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureControl, { once: true });
  } else {
    ensureControl();
  }
})();
