/**
 * Theme switch — the ONE place that flips <html> between the dark default
 * (class="dark dark-theme" data-theme="dark", set by index.html) and the
 * light overrides (index.css `.light, .light-theme, :root[data-theme="light"]`).
 * Persisted in localStorage("prism-theme"); applied on module load so the
 * choice survives reloads without a flash of the wrong canvas.
 */

export type Theme = "dark" | "light";

const KEY = "prism-theme";

export function currentTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(t: Theme): void {
  const el = document.documentElement;
  el.classList.remove("dark", "dark-theme", "light", "light-theme");
  el.classList.add(t, `${t}-theme`);
  el.setAttribute("data-theme", t);
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* private mode — theme still applies for this load */
  }
}

export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  return next;
}

// Apply the persisted choice at import time (main.tsx imports this module).
applyTheme(currentTheme());
