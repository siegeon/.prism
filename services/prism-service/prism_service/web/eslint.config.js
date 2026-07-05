// Flat ESLint config — intentionally minimal. Its ONE job is the PRISM
// palette guard: forbid raw Tailwind color utilities so every domain
// color-coding decision routes through the --accent-{tone}-{bg,ring,fg}
// tokens in src/index.css (resolved via src/lib/domainTone.ts). We do
// NOT enable the typescript-eslint recommended sets here — that would
// light up the whole SPA with unrelated churn; the palette rule is the
// contract this config enforces.
import tsparser from "@typescript-eslint/parser";

// Raw Tailwind color utilities that bypass the token palette. Matches the
// task guard exactly: (text|bg|border|ring|from|to)-<tone>-<shade> for the
// twelve color families that alias one of our seven tones, plus any bare
// cyan-* (a hue that isn't in the palette at all — must become teal).
const RAW_COLOR =
  /(?:text|bg|border|ring|from|to)-(?:amber|rose|emerald|teal|violet|sage|slate|cyan|green|red|yellow|blue)-[0-9]/;
const BARE_CYAN = /\bcyan-/;

/** @type {import('eslint').Rule.RuleModule} */
const noRawTailwindColor = {
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow raw Tailwind color utilities; use --accent-{tone}-{bg,ring,fg} tokens via lib/domainTone.ts.",
    },
    messages: {
      raw: "Raw Tailwind color utility '{{match}}' bypasses the palette. Use the --accent-{tone}-{bg,ring,fg} tokens (see lib/domainTone.ts).",
    },
    schema: [],
  },
  create(context) {
    const scan = (node, text) => {
      if (typeof text !== "string") return;
      const m = RAW_COLOR.exec(text) || BARE_CYAN.exec(text);
      if (m) context.report({ node, messageId: "raw", data: { match: m[0] } });
    };
    return {
      Literal(node) {
        if (typeof node.value === "string") scan(node, node.value);
      },
      TemplateElement(node) {
        scan(node, node.value && node.value.raw);
      },
    };
  },
};

const prismPlugin = { rules: { "no-raw-tailwind-color": noRawTailwindColor } };

// A couple of source files carry `// eslint-disable-next-line
// react-hooks/exhaustive-deps` directives. This config intentionally does
// not run the react-hooks ruleset (it would flood the SPA with unrelated
// findings), but an unresolved rule name in a disable directive is itself a
// hard error. Register the referenced names as no-op rules so the
// directives resolve cleanly without enabling any react-hooks linting.
const noop = { create: () => ({}) };
const reactHooksStub = {
  rules: { "exhaustive-deps": noop, "rules-of-hooks": noop },
};

export default [
  {
    ignores: [
      "dist/**",
      "web_dist/**",
      "node_modules/**",
      "**/*.tsbuildinfo",
      "eslint.config.js",
      "vite.config.ts",
    ],
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    linterOptions: { reportUnusedDisableDirectives: "off" },
    plugins: { prism: prismPlugin, "react-hooks": reactHooksStub },
    rules: { "prism/no-raw-tailwind-color": "error" },
  },
  {
    // Intentional JS hex mirrors of the CSS tokens — canvas/SVG/Sigma can't
    // read CSS vars, so these author the same palette as raw values. Excepted
    // from the guard (index.css is not linted; it's not a JS/TS file).
    files: ["src/lib/palette.ts", "src/components/Chart.tsx"],
    rules: { "prism/no-raw-tailwind-color": "off" },
  },
];
