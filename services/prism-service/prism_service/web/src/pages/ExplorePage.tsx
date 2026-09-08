// ===========================================================================
// Explore — THE CODE GRAPH. That is the whole page.
//
// It used to be the brain-understand page wearing Explore's name, and the
// furniture stayed long after the content changed: an "Ask the graph" search
// over docs/expertise/memory, py/ts/md/expertise domain pills, Reindex /
// Rebuild / Enrich, and a nodes/edges/communities/ranked strip whose "131
// COMMUNITIES" sat on the same screen as the map's own "12 communities" —
// two numbers for one word. Owner: "you say explore in 3-4 places ... buttons
// we never asked for in a panal we dont need showing data that is not code.
// wioth py ts md etc... and a search bar that has nothing to do wuth the
// code", and before that "it seems to me you confused understand (concepts
// and memory) with the code graph".
//
// All of it is gone. Searching docs, concepts and memory is Understand's job
// and it is one nav item away. What remains:
//   - no focus  -> the archify embed, filling the page
//   - ?focus=   -> the mesh, centred on that entity (any xref token)
//   - Full map  -> the Sigma whole-graph canvas, a deliberate opt-in
//
// ?focus= is now read ONE way, as a generic xref token handed to the mesh.
// The old second reading — treat it as a FILE and seed the brain-understand
// endpoint with it — is what made /brain?focus=<task-uuid> echo the raw id
// back as a phantom kind:"file" node; that whole class of bug leaves with
// the search.
//
// A node click used to ambush the reader: the old onMapNode turned any click
// straight into onPick(target), which set ?focus= and swapped the whole
// embed for <Mesh> before the reader ever touched the artifact's own
// Semantic Passport, Route Probe, Semantic Lens, chapter rail or export
// menu — so the embed unmounted on the FIRST click and the reader never
// reached its own interactions. Owner: it "just has one single dashboard
// that is not really interactive or useful."
//
// Now a click only RECORDS the pick; <ArchifyMaps> stays mounted, and the
// reader gets an explicit "Open … in mesh" door instead of an involuntary
// jump. A lens strip (Code / Concepts / Language) and a Delta toggle sit
// beside that door, both talking to the SAME mounted embed via the `kind`
// and `view` props — one surface, read three ways, never a second panel
// stacked on top of it.
// ===========================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Map as MapIcon } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import ArchifyMaps, { type ArchifyKind, type ArchifyView } from "@/components/maps/ArchifyMaps";
import { Card, Empty, SectionLabel } from "@/components/ui";
import Mesh from "@/components/Mesh";
import { cn } from "@/lib/utils";

// The lens strip Explore owns. ArchifyMaps carries the same three tabs
// internally, but every caller has always pinned a fixed `kind`, which is
// why that strip never rendered anywhere — this is what turns it on.
const LENS: { kind: ArchifyKind; label: string }[] = [
  { kind: "code", label: "Code" },
  { kind: "concepts", label: "Concepts" },
  { kind: "language", label: "Language" },
];

export default function ExplorePage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const focus = sp.get("focus");
  // True only once the owner clicks "Full map" — the one way to the Sigma
  // canvas. Any subsequent focus clears it so the mesh takes back over.
  const [wantFullMap, setWantFullMap] = useState(false);
  const [hops, setHops] = useState<1 | 2>(2);
  const [viewerUrl, setViewerUrl] = useState<string | null>(null);

  // Which map the embed draws, and whether it draws the map or the delta.
  const [lens, setLens] = useState<ArchifyKind>("code");
  const [view, setView] = useState<ArchifyView>("map");
  // The delta 404s until a task land has replaced a map — absent, not
  // broken. The embed probes it and reports back through this callback.
  const [deltaAvailable, setDeltaAvailable] = useState(false);
  // A node click lands here, not on ?focus=. The reader opts into the mesh
  // through the door this renders, instead of being thrown into it.
  const [picked, setPicked] = useState<{ nodeId: string; kind: ArchifyKind; target?: string } | null>(null);

  const setFocus = useCallback((token: string | null, opts?: { replace?: boolean }) => {
    if (token) setWantFullMap(false);
    setSp((prev) => {
      const n = new URLSearchParams(prev);
      if (token) n.set("focus", token); else n.delete("focus");
      return n;
    }, opts);
  }, [setSp]);

  // /brain?session=<id> / ?task=<id> — the /live graph's explore hop.
  // Normalised once into the mesh's own ?focus= token space.
  const paramFocusSeed = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    return p.get("session") || p.get("task") || null;
  }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (paramFocusSeed && !focus) setFocus(paramFocusSeed, { replace: true }); }, []);

  // NO AUTO-FOCUS. A bare visit used to probe the board and silently centre
  // the mesh on whichever task moved last, so Explore opened on a task nobody
  // asked for and the architecture flashed past on the way.

  useEffect(() => {
    api.get<{ graph_json_exists: boolean; viewer_url: string }>(`/api/graph/summary?project=${project}`)
      .then((s) => setViewerUrl(s.graph_json_exists ? s.viewer_url : null))
      .catch(() => setViewerUrl(null));
  }, [project]);

  // If the delta goes unavailable (a project switch, a fresh checkout with
  // nothing landed yet) while it is on screen, fall back to the map rather
  // than hold a dead view open.
  useEffect(() => { if (view === "delta" && !deltaAvailable) setView("map"); }, [deltaAvailable]);

  // A node in the drawing was clicked. This used to call onPick(target)
  // straight away, which set ?focus= and swapped <ArchifyMaps> for <Mesh>
  // before the reader touched anything else in the embed. It only records
  // the pick now; opening the mesh is a separate, explicit click below.
  const onMapNode = (nodeId: string, kind: string, target?: string) => {
    setPicked({ nodeId, kind: kind as ArchifyKind, target });
  };

  const openPickedInMesh = () => {
    if (picked?.target) { setFocus(picked.target); setPicked(null); }
  };

  const meshDoorLabel = picked?.target ? picked.target.split("/").pop() : picked?.nodeId;

  return (
    <div className="h-full flex flex-col px-5 py-4 min-w-[720px]">
      <Card className="!p-0 overflow-hidden flex-1 min-h-0 flex flex-col">
        {/* The page header already says Explore; this row says only what is
            on screen and what can be done to it. */}
        {focus ? (
          <div className="px-5 pt-3 pb-2 flex items-center gap-2 shrink-0">
            <SectionLabel>Focused</SectionLabel>
            <span className="text-xs opacity-50 ml-1">
              Knowledge links knowledge, code calls code, sessions touch gates.
              Click a node to wander, double-click to open it.
            </span>
            <div className="ml-auto flex items-center gap-2">
              <div className="inline-flex rounded-md border border-[color:var(--border-default)] overflow-hidden">
                {([1, 2] as const).map((h) => (
                  <button key={h} onClick={() => setHops(h)}
                    title={h === 1 ? "Direct neighbors only" : "Neighbors and their neighbors"}
                    className={cn("px-2.5 py-1 text-2xs uppercase tracking-wider transition-colors",
                      hops === h
                        ? "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]"
                        : "bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-[color:var(--text-secondary)]")}>
                    {h} hop{h === 1 ? "" : "s"}
                  </button>
                ))}
              </div>
              <button onClick={() => { setWantFullMap(true); setFocus(null); }}
                title="Open the full graph canvas"
                className="inline-flex items-center gap-1 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-2 py-1 text-2xs uppercase tracking-wider">
                <MapIcon className="w-3 h-3" /> Full map
              </button>
              <button onClick={() => setFocus(null)}
                className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100">
                ← architecture
              </button>
            </div>
          </div>
        ) : wantFullMap ? (
          <div className="px-5 pt-3 pb-2 flex items-center gap-2 shrink-0">
            <SectionLabel>Full map</SectionLabel>
            <span className="text-xs opacity-50 ml-1">the whole graph, coloured by community</span>
            <button onClick={() => setWantFullMap(false)}
              className="ml-auto text-2xs uppercase tracking-wider opacity-60 hover:opacity-100">
              ← architecture
            </button>
          </div>
        ) : (
          <div className="px-5 pt-3 pb-2 flex items-center gap-2 shrink-0">
            <div className="inline-flex rounded-md border border-[color:var(--border-default)] overflow-hidden">
              {LENS.map((t) => (
                <button key={t.kind} type="button" onClick={() => { setLens(t.kind); setPicked(null); }}
                  title={`Draw the ${t.label.toLowerCase()} map`}
                  className={cn("px-2.5 py-1 text-2xs uppercase tracking-wider transition-colors",
                    lens === t.kind
                      ? "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]"
                      : "bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-[color:var(--text-secondary)]")}>
                  {t.label}
                </button>
              ))}
            </div>
            <button type="button" onClick={() => { setView(view === "delta" ? "map" : "delta"); setPicked(null); }}
              disabled={!deltaAvailable}
              aria-pressed={view === "delta"}
              title={deltaAvailable ? "Show what the last land changed" : "No delta yet — nothing has replaced a map"}
              className={cn("px-2.5 py-1 text-2xs uppercase tracking-wider rounded-md border border-[color:var(--border-default)] disabled:opacity-40 disabled:cursor-not-allowed",
                view === "delta"
                  ? "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]"
                  : "bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-[color:var(--text-secondary)]")}>
              Delta
            </button>
            {picked?.target && (
              <button type="button" onClick={openPickedInMesh}
                title="Centre the mesh on this node"
                className="inline-flex items-center gap-1 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-2 py-1 text-2xs uppercase tracking-wider">
                Open {meshDoorLabel} in mesh
              </button>
            )}
            <button onClick={() => setWantFullMap(true)}
              title="Open the full graph canvas"
              className="ml-auto inline-flex items-center gap-1 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-2 py-1 text-2xs uppercase tracking-wider">
              <MapIcon className="w-3 h-3" /> Full map
            </button>
          </div>
        )}

        {focus ? (
          <Mesh token={focus} project={project} hops={hops} onFocus={setFocus}
            onOpen={(href) => navigate(href)} />
        ) : wantFullMap ? (
          viewerUrl ? (
            <iframe src={viewerUrl} title="graph canvas"
              className="w-full flex-1 min-h-0 border-0"
              style={{ background: "#0f0f1a" }} />
          ) : (
            <div className="px-5 pb-5"><Empty>No graph yet — rebuild the map below.</Empty></div>
          )
        ) : (
          <div className="px-5 pb-5 flex-1 min-h-0 flex flex-col">
            {lens === "code" ? (
              <ArchifyMaps project={project} kind="code" view={view}
                onDeltaAvailability={setDeltaAvailable}
                onNodeSelect={onMapNode} focusId={picked?.target ?? picked?.nodeId} fill />
            ) : (
              <ArchifyMaps project={project} kind={lens} view={view}
                onDeltaAvailability={setDeltaAvailable}
                onNodeSelect={onMapNode} focusId={picked?.target ?? picked?.nodeId} fill />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
