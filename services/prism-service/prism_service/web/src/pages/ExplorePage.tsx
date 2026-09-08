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
//   - no focus  -> the code architecture, filling the page
//   - ?focus=   -> the mesh, centred on that entity (any xref token)
//   - Full map  -> the Sigma whole-graph canvas, a deliberate opt-in
//
// ?focus= is now read ONE way, as a generic xref token handed to the mesh.
// The old second reading — treat it as a FILE and seed the brain-understand
// endpoint with it — is what made /brain?focus=<task-uuid> echo the raw id
// back as a phantom kind:"file" node; that whole class of bug leaves with
// the search.
// ===========================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Map as MapIcon } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import ArchifyMaps from "@/components/maps/ArchifyMaps";
import { Card, Empty, SectionLabel } from "@/components/ui";
import Mesh from "@/components/Mesh";
import { cn } from "@/lib/utils";

// Only the fields the cluster click-through reads. The fuller Community shape
// belonged to the ranked/subgraph panels, which no longer exist.
type Community = { id: number; label: string; top_entities: string[] };

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
        ) : null}

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
          <StartHere project={project} onPick={(token) => setFocus(token)} />
        )}
      </Card>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// StartHere — THE FRONT DOOR IS THE ARCHITECTURE. Explore is the code graph,
// so a bare visit draws the code graph, filling the card. Clicking a
// component centres the mesh on the code behind it.
// ─────────────────────────────────────────────────────────────────────────
function StartHere({ project, onPick }: {
  project: string; onPick: (token: string) => void;
}) {
  const [communities, setCommunities] = useState<Community[]>([]);

  useEffect(() => {
    let alive = true;
    // A click has to land on a real token, and a cluster id is not one.
    api.get<{ communities: Community[] }>(`/api/graph/communities?project=${project}`)
      .then((r) => { if (alive) setCommunities(r.communities ?? []); })
      .catch(() => { if (alive) setCommunities([]); });
    return () => { alive = false; };
  }, [project]);

  // archify draws each component with id `c<communityId>-<slugged label>`.
  const onMapNode = (nodeId: string) => {
    const m = /^c(\d+)-/.exec(nodeId);
    if (!m) return;
    const c = communities.find((x) => String(x.id) === m[1]);
    const token = c?.top_entities?.find((e) => e && e.trim());
    if (token) onPick(token.replace(/\(\)$/, ""));
  };

  return (
    <div className="px-5 pb-5 flex-1 min-h-0 flex flex-col">
      <ArchifyMaps project={project} kind="code" onNodeSelect={onMapNode} fill />
    </div>
  );
}
