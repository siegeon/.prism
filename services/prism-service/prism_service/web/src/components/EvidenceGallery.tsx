// Evidence gallery + lightbox — the one place PRISM renders a task's captured
// artifacts (screenshots / videos) so an approver can SEE the proof, not just
// read a claim. Shared by two callers: the Implementation tab's right-hand
// Evidence panel (the full store, via EvidencePanel) and the StepRail gate
// rows (the subset a gate receipt cites). A dedicated module because the
// lightbox is reused across those two files — extending either owner would
// force the other to import a page/rail internal.
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

export type EvidenceItem = {
  url: string;
  name: string;
  // "text" covers a log/diff/receipt (task 48ee8f05). Without it a cited .txt
  // fell through to <img src=...txt> and painted a BROKEN TILE on the gate
  // panel — evidence that cannot be read is not evidence.
  kind?: "image" | "video" | "text";
  caption?: string;
  cited?: boolean;
};

type ApiEvidenceFile = {
  name: string;
  url: string;
  media_type: string;
  kind: "image" | "video" | "text";
  size_bytes: number;
  modified: string;
  cited: boolean;
};

// A log/diff/receipt tile. Fetches the artifact and shows it as readable text
// (task 48ee8f05) — the gallery used to hand every non-video item to <img>, so
// a cited .txt rendered as a broken image on the gate panel.
function TextArtifact({ url, full }: { url: string; full?: boolean }) {
  const [body, setBody] = useState<string>("");
  useEffect(() => {
    let cancelled = false;
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((t) => { if (!cancelled) setBody(t); })
      .catch(() => { if (!cancelled) setBody("(could not read artifact)"); });
    return () => { cancelled = true; };
  }, [url]);
  return (
    <pre
      className={"m-0 p-2 font-mono whitespace-pre-wrap break-words " +
        (full
          ? "text-[12px] leading-relaxed max-h-[80vh] overflow-auto"
          : "text-2xs leading-snug h-full overflow-hidden")}
      style={{ background: "var(--surface-1)", color: "var(--text-secondary)" }}
    >
      {body || "loading…"}
    </pre>
  );
}

function fmtSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// A thumbnail grid whose tiles open a fullscreen lightbox. `thumb="sm"` is the
// compact strip used inside a gate row; the default is the panel grid.
export function EvidenceGallery({ items, thumb = "md", className }: {
  items: EvidenceItem[];
  thumb?: "sm" | "md";
  className?: string;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const [zoom, setZoom] = useState(false);
  useEffect(() => { setZoom(false); }, [open]);
  useEffect(() => {
    if (open === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
      if (e.key === "ArrowRight") setOpen((v) => (v === null ? v : (v + 1) % items.length));
      if (e.key === "ArrowLeft") setOpen((v) => (v === null ? v : (v + items.length - 1) % items.length));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, items.length]);

  if (items.length === 0) return null;
  const shown = open !== null ? items[open] : null;
  const gridCls = thumb === "sm"
    ? "flex flex-wrap gap-2"
    : "grid grid-cols-1 min-[420px]:grid-cols-2 gap-2.5";
  const tileH = thumb === "sm" ? "h-16" : "max-h-[200px]";

  return (
    <div className={`${gridCls} ${className ?? ""}`}>
      {items.map((it, i) => (
        <button
          key={it.url}
          type="button"
          onClick={() => setOpen(i)}
          className={"group block text-left min-w-0 rounded-md border border-[color:var(--border-default)] overflow-hidden hover:border-[color:var(--accent-teal-ring)] transition-colors " + (thumb === "sm" ? "w-24 shrink-0" : "")}
          title={`${it.name} — click to view`}
        >
          <div className="relative bg-[color:var(--surface-3)] grid place-items-center">
            {it.kind === "video" ? (
              <>
                <video src={it.url} preload="metadata" muted
                  className={`w-full ${tileH} object-cover`} />
                <span className="absolute inset-0 grid place-items-center text-lg" style={{ color: "rgba(255,255,255,0.85)" }} aria-hidden>▶</span>
              </>
            ) : it.kind === "text" ? (
              <div className={`w-full ${tileH} overflow-hidden`}>
                <TextArtifact url={it.url} />
              </div>
            ) : (
              <img src={it.url} alt={it.caption || it.name} loading="lazy"
                className={`w-full ${tileH} object-cover`} />
            )}
            {it.cited && (
              <span className="absolute top-1 left-1 text-[11px] uppercase tracking-wider px-1 py-0.5 rounded font-semibold"
                style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
                title="referenced by this task's completion proof">cited</span>
            )}
          </div>
          {thumb === "md" && (
            <div className="px-2 py-1.5">
              <div className="font-mono text-2xs truncate" style={{ color: "var(--text-secondary)" }}>{it.name}</div>
              {it.caption && (
                <div className="text-2xs mt-0.5 leading-snug line-clamp-2" style={{ color: "var(--text-muted)" }}>{it.caption}</div>
              )}
            </div>
          )}
        </button>
      ))}

      {shown && (
        <div className="fixed inset-0 z-50" style={{ background: "rgba(0,0,0,0.88)" }}
          role="dialog" aria-modal="true" aria-label={shown.name} onClick={() => setOpen(null)}>
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 p-2">
            {shown.kind === "video" ? (
              <video src={shown.url} controls autoPlay
                className="max-w-[98vw] max-h-[92vh] rounded-sm shadow-2xl"
                onClick={(e) => e.stopPropagation()} />
            ) : shown.kind === "text" ? (
              // A log opens as SCROLLABLE TEXT, never the zoomable image viewer.
              <div
                className="max-w-[98vw] w-[min(1100px,98vw)] max-h-[92vh] overflow-auto rounded-sm shadow-2xl border border-[color:var(--border-default)]"
                onClick={(e) => e.stopPropagation()}
              >
                <TextArtifact url={shown.url} full />
              </div>
            ) : zoom ? (
              <div className="absolute inset-0 overflow-auto" onClick={() => setOpen(null)}>
                <img src={shown.url} alt={shown.name}
                  className="block mx-auto my-4 cursor-zoom-out max-w-none"
                  onClick={(e) => { e.stopPropagation(); setZoom(false); }} />
              </div>
            ) : (
              <img src={shown.url} alt={shown.name}
                className="max-w-[98vw] max-h-[92vh] object-contain rounded-sm shadow-2xl cursor-zoom-in"
                onClick={(e) => { e.stopPropagation(); setZoom(true); }} />
            )}
            <div className="flex flex-col items-center gap-1 max-w-[80vw]">
              <div className="font-mono text-2xs" style={{ color: "rgba(255,255,255,0.85)" }}>{shown.name}</div>
              {shown.caption && (
                <div className="text-[13px] leading-relaxed text-center" style={{ color: "rgba(255,255,255,0.92)" }}>{shown.caption}</div>
              )}
              <div className="flex items-center gap-3 text-2xs" style={{ color: "rgba(255,255,255,0.6)" }}>
                {items.length > 1 && <span className="font-mono tabular-nums">{(open ?? 0) + 1} / {items.length} · ← →</span>}
                <span className="uppercase tracking-wider shrink-0">
                  {shown.kind === "video" ? "esc closes" : "click image to zoom · esc closes"}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Fetch a task's whole evidence store (GET /api/tasks/:id/evidence) as gallery
// items. Callers render them where the evidence belongs — inside the timeline
// gate row that the artifact passed — not in a detached panel. `caption`
// carries kind + size; `cited` marks files the completion proof references.
export function useTaskEvidence(taskId?: string, project = "default"): EvidenceItem[] {
  const [files, setFiles] = useState<ApiEvidenceFile[]>([]);
  useEffect(() => {
    if (!taskId) { setFiles([]); return; }
    let live = true;
    api.get<{ files: ApiEvidenceFile[] }>(`/api/tasks/${taskId}/evidence?project=${project}`)
      .then((d) => { if (live) setFiles(d.files ?? []); })
      .catch(() => { if (live) setFiles([]); });
    return () => { live = false; };
  }, [taskId, project]);

  return useMemo(
    () => files.map((f) => ({
      url: f.url, name: f.name, kind: f.kind, cited: f.cited,
      caption: `${f.kind === "video" ? "video" : f.kind === "text" ? "log" : "screenshot"}${fmtSize(f.size_bytes) ? " · " + fmtSize(f.size_bytes) : ""}`,
    })),
    [files],
  );
}

// Extract /api/tasks/:id/evidence/<file> citations from a receipt/proof string
// so a gate row can show the specific artifacts it produced. Real linkage: the
// filename is already written into the proof text, we just surface it inline.
export function citedEvidence(text?: string): EvidenceItem[] {
  const seen = new Set<string>();
  const out: EvidenceItem[] = [];
  const re = /\/api\/tasks\/[A-Za-z0-9_-]+\/evidence\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/g;
  for (const m of (text ?? "").matchAll(re)) {
    const url = m[0];
    if (seen.has(url)) continue;
    seen.add(url);
    const name = m[1];
    // THIS is the path a completion-proof citation takes. It used to call
    // everything that was not video an "image", so `![pytest run](run.txt)`
    // became an <img> pointing at a log — the broken tile on the gate panel.
    const kind: "image" | "video" | "text" =
      /\.(mp4|webm)$/i.test(name) ? "video"
        : /\.(txt|log|diff|patch|md|json)$/i.test(name) ? "text"
          : "image";
    out.push({ url, name, kind });
  }
  return out;
}
