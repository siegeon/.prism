import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import fs from "fs";

const BACKEND = process.env.PRISM_BACKEND_URL ?? "http://127.0.0.1:7778";
const WATCH_BUILD = process.env.PRISM_WATCH_BUILD === "1";
const WEB_DIST = path.resolve(__dirname, "../web_dist");
const WATCH_STAGE = path.resolve(__dirname, "../web_dist_next");

/** Publish a watched build as one coherent generation. Entry HTML is the
 * commit point: every hashed asset it names is copied before the atomic
 * rename, while old hashed assets remain available to already-open tabs. */
function atomicWatchPublisher() {
  return {
    name: "prism-atomic-watch-publisher",
    closeBundle() {
      if (!WATCH_BUILD) return;
      fs.mkdirSync(WEB_DIST, { recursive: true });
      for (const entry of fs.readdirSync(WATCH_STAGE)) {
        if (entry === "index.html") continue;
        fs.cpSync(path.join(WATCH_STAGE, entry), path.join(WEB_DIST, entry), {
          recursive: true,
          force: true,
        });
      }
      const nextIndex = path.join(WEB_DIST, ".index.html.next");
      fs.copyFileSync(path.join(WATCH_STAGE, "index.html"), nextIndex);
      fs.renameSync(nextIndex, path.join(WEB_DIST, "index.html"));
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), atomicWatchPublisher()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
    dedupe: [
      "react",
      "react-dom",
      "@react-three/fiber",
      "@observablehq/plot",
      "three",
      "leva",
      "gsap",
    ],
  },
  build: {
    outDir: WATCH_BUILD ? WATCH_STAGE : WEB_DIST,
    // Watched builds may freely clear staging. The served directory changes
    // only in atomicWatchPublisher after a complete generation exists.
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Pull the rarely-changing React runtime into its own chunk so it
        // caches across deploys and trims the per-build main chunk.
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: BACKEND, ws: true },
      "/sse": { target: BACKEND, ws: true },
      "/graph": { target: BACKEND },
    },
  },
  // Aspire exposes `vite preview` as the durable UI listener. Python may be
  // restarted independently; document/assets remain available and only API
  // requests degrade until the backend returns.
  preview: {
    proxy: {
      "/api": { target: BACKEND, ws: true },
      "/sse": { target: BACKEND, ws: true },
      "/graph": { target: BACKEND },
      "/graphify-visual": { target: BACKEND },
      "/integrations/webhooks": { target: BACKEND },
    },
  },
});
