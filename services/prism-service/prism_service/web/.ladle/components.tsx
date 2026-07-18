/**
 * Ladle's theme hook (task d30c9a75, AC-2 — the #1 misfire). Every story
 * mounts through this Provider, so it's the ONE place that has to wire
 * stories into the app's real theme layer instead of a bare unstyled
 * render:
 *   1. Import the app's index.css (Tailwind v4 + Radix scales + the
 *      Hermes token set) so `--surface-*`/`--text-*`/`--accent-*` exist.
 *   2. Wrap children in a div carrying the SAME class + data-theme pair
 *      index.html puts on <html> ("dark dark-theme" / "light light-theme").
 *      index.css's theme selectors (`.light, .light-theme,
 *      :root[data-theme="light"]`) are plain class selectors, not
 *      :root-only, so scoping them to this wrapper div works whether a
 *      story renders inline (Ladle's default "full" mode, same document
 *      as the shell) or inside Ladle's story iframe (width/iframed
 *      meta) — no document.documentElement reach-in required either way.
 *
 * ONE palette source: nothing here re-declares a color — it only toggles
 * which half of index.css's tokens are active, same as the real app.
 */
import type { ReactNode } from "react";
import type { GlobalProvider } from "@ladle/react";
import "../src/index.css";

export const Provider: GlobalProvider = ({ children, globalState }) => {
  // globalState.theme can be "light" | "dark" | "auto" (config.mjs sets
  // the default to "dark" to match index.html; "auto" still needs
  // resolving against the OS preference the way Ladle's own init script
  // resolves it for the shell chrome).
  const theme =
    globalState.theme === "auto"
      ? window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : globalState.theme;

  return (
    <div
      data-theme={theme}
      className={theme === "light" ? "light light-theme" : "dark dark-theme"}
      style={{
        minHeight: "100vh",
        padding: "1.5rem",
        background: "var(--background-base)",
        color: "var(--midground-base)",
      }}
    >
      {children as ReactNode}
    </div>
  );
};
