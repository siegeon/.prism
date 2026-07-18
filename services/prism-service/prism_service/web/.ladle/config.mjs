/**
 * Ladle config (task d30c9a75). Ladle auto-discovers ../vite.config.ts
 * from process.cwd() (ladleConfig.viteConfig stays undefined -> Vite's
 * loadConfigFromFile searches cwd) and merges its plugins array in, so
 * @tailwindcss/vite and the "@" path alias are already live here — no
 * duplicate plugin wiring needed. See .ladle/components.tsx for the
 * theme wrapper that makes stories render through the real token layer.
 *
 * @type {import('@ladle/react').UserConfig}
 */
export default {
  stories: "src/**/*.stories.{ts,tsx}",
  addons: {
    // PRISM's index.html boots dark by default (no light/light-theme
    // class); match that here so the workshop's first paint is the
    // theme the app actually ships, not Ladle's own "light" default.
    theme: {
      defaultState: "dark",
    },
  },
};
