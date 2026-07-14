// check-contrast.mjs — task 401811b8 oracle.
// Fetches the CSS the LIVE dev daemon serves (default 127.0.0.1:8888),
// resolves the --accent-{tone}-{bg,fg} token pairs for BOTH themes
// (light + dark), composites alpha fills over the theme surface and
// asserts every lozenge text/bg pair meets WCAG AA >= 4.5:1. Also
// asserts the amber/warn SOLID surface takes DARK text.
// Exit 0 = every pair passes in both themes; exit 1 otherwise.
const BASE = process.env.PRISM_BASE || "http://127.0.0.1:8888";
const TONES = ["teal", "sage", "amber", "rose", "violet", "emerald", "slate"];
const AA = 4.5;

async function text(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`GET ${url} -> ${r.status}`);
  return r.text();
}

async function servedCss() {
  const html = await text(BASE + "/");
  const hrefs = [...html.matchAll(/href="([^"]+\.css)"/g)].map((m) => m[1]);
  if (!hrefs.length) throw new Error("no CSS bundle referenced by " + BASE);
  let css = "";
  for (const h of hrefs) css += await text(BASE + (h.startsWith("/") ? h : "/" + h));
  return css;
}

// Drop @media/@supports/@keyframes bodies (balanced braces) so only
// top-level rules remain — the P3 color(display-p3 …) overrides and
// prefers-* blocks would otherwise poison the token maps.
function stripAtBlocks(css) {
  let out = "", i = 0;
  while (i < css.length) {
    if (css[i] === "@" && /^@(media|supports|keyframes|layer\s*[^;{]*\{)/.test(css.slice(i, i + 40))) {
      while (i < css.length && css[i] !== "{") i++;
      let depth = 1; i++;
      while (i < css.length && depth > 0) {
        if (css[i] === "{") depth++;
        else if (css[i] === "}") depth--;
        i++;
      }
    } else out += css[i++];
  }
  return out;
}

// Collect custom-property declarations per theme. Base = every :root
// rule (radix LIGHT scales bind on `:root, .light, .light-theme`);
// dark overlays .dark/.dark-theme/[data-theme="dark"] rules, light
// overlays .light/.light-theme/[data-theme="light"] rules.
function themeVars(css, theme) {
  const vars = {};
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  const wants =
    theme === "dark" ? /:root|\.dark|data-theme="dark"/ : /:root|\.light|data-theme="light"/;
  const rejects =
    theme === "dark" ? /\.light|data-theme="light"/ : /\.dark(?![a-z-])|\.dark-theme|data-theme="dark"/;
  for (const [, sel, body] of css.matchAll(rule)) {
    if (!wants.test(sel) || rejects.test(sel)) continue;
    for (const [, k, v] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+)/g)) vars[k] = v.trim();
  }
  return vars;
}

function resolve(value, vars, depth = 0) {
  if (depth > 12) throw new Error("var() chain too deep: " + value);
  const m = value && value.match(/^var\((--[\w-]+)\)$/);
  if (!m) return value;
  if (!(m[1] in vars)) throw new Error("undefined token " + m[1]);
  return resolve(vars[m[1]], vars, depth + 1);
}

function parseColor(v) {
  let m = v.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/i);
  if (m) {
    const h = m[1];
    return {
      r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16), a: m[2] ? parseInt(m[2], 16) / 255 : 1,
    };
  }
  m = v.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)$/);
  if (m) return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  throw new Error("unparseable color: " + v);
}

const over = (fg, bg) => ({
  r: fg.a * fg.r + (1 - fg.a) * bg.r,
  g: fg.a * fg.g + (1 - fg.a) * bg.g,
  b: fg.a * fg.b + (1 - fg.a) * bg.b,
  a: 1,
});

function luminance({ r, g, b }) {
  const f = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const css = stripAtBlocks(await servedCss());
let failed = 0;
const row = (theme, name, ratio, extra = "") => {
  const ok = ratio >= AA;
  if (!ok) failed++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${theme.padEnd(5)} ${name.padEnd(22)} ${ratio.toFixed(2)}:1 (AA >= ${AA}:1)${extra}`,
  );
};

for (const theme of ["light", "dark"]) {
  const vars = themeVars(css, theme);
  // Lozenges sit on white in the light theme, on --surface-1 in dark.
  const surface =
    theme === "dark" ? parseColor(resolve("var(--surface-1)", vars)) : parseColor("#ffffff");
  for (const tone of TONES) {
    const fg = parseColor(resolve(`var(--accent-${tone}-fg)`, vars));
    const bg = over(parseColor(resolve(`var(--accent-${tone}-bg)`, vars)), surface);
    row(theme, `accent-${tone} fg/bg`, contrast(fg, bg));
  }
  // Amber/warn SOLID surfaces must take DARK text in both themes.
  const sfg = parseColor(resolve("var(--accent-amber-solid-fg)", vars));
  const sbg = parseColor(resolve("var(--accent-amber-solid-bg)", vars));
  const darkText = luminance(sfg) < luminance(sbg);
  if (!darkText) failed++;
  row(theme, "amber-solid fg/bg", contrast(sfg, sbg), darkText ? " dark-text" : " LIGHT-TEXT-ON-AMBER");
}

console.log(failed ? `\n${failed} pair(s) below AA — FAIL` : "\nall lozenge pairs pass AA in both themes");
// process.exit() trips a libuv teardown assertion on Windows node while
// undici keep-alive handles unwind — set exitCode and drain instead.
process.exitCode = failed ? 1 : 0;
