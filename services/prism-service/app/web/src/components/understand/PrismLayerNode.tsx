/**
 * PrismLayerNode — layer node renderer for the architecture graph.
 *
 * Structure ported from Lum1104/Understand-Anything's LayerClusterNode
 * (understand-anything-plugin/packages/dashboard/src/components/
 * LayerClusterNode.tsx @ sha 57a25ed4, MIT). Reskinned to Hermes
 * (PRISM design tokens, font-serif headings, no UA-specific store).
 *
 * Copyright (c) 2026 Yuxiang Lin (MIT) — original structure
 * Adapted for PRISM under MIT-compatible terms.
 */
import { memo } from "react";
import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";

export interface PrismLayerNodeData extends Record<string, unknown> {
  layerId: string;
  name: string;
  description?: string;
  complexity?: "simple" | "moderate" | "complex";
  file_count?: number;
  colorIdx: number;
  selected?: boolean;
}

export type PrismLayerFlowNode = Node<PrismLayerNodeData, "prism-layer">;

// Layer palette — v5.2.4 refresh. Previous version sat all 7 swatches
// inside the cool blue-grey family (Slate Blue theme harmony) but that
// made every layer read identically in practice — a 10-layer architecture
// graph looked like one blob. New palette gives each layer a real hue
// while keeping saturation tasteful for the dark theme (each ~L65/C25 in
// OKLCH terms — distinct but never crayon-bright).
const PALETTE = [
  { border: "rgba(74,124,155,0.55)",  bg: "rgba(74,124,155,0.10)",  dot: "#4a7c9b" },  // steel blue
  { border: "rgba(95,170,180,0.55)",  bg: "rgba(95,170,180,0.10)",  dot: "#5faab4" },  // teal
  { border: "rgba(120,170,130,0.55)", bg: "rgba(120,170,130,0.10)", dot: "#78aa82" },  // sage green
  { border: "rgba(200,170,90,0.55)",  bg: "rgba(200,170,90,0.10)",  dot: "#c8aa5a" },  // amber
  { border: "rgba(220,135,110,0.55)", bg: "rgba(220,135,110,0.10)", dot: "#dc876e" },  // coral
  { border: "rgba(170,140,200,0.55)", bg: "rgba(170,140,200,0.10)", dot: "#aa8cc8" },  // lavender
  { border: "rgba(210,130,165,0.55)", bg: "rgba(210,130,165,0.10)", dot: "#d282a5" },  // rose
  { border: "rgba(160,150,135,0.55)", bg: "rgba(160,150,135,0.10)", dot: "#a09687" },  // warm grey
];
export const prismLayerColor = (i: number) => PALETTE[i % PALETTE.length];


const COMPLEXITY_LABEL: Record<string, string> = {
  simple: "simple", moderate: "moderate", complex: "complex",
};


function PrismLayerNode({ data, selected }: NodeProps<PrismLayerFlowNode>) {
  const color = prismLayerColor(data.colorIdx);
  return (
    <div
      className="rounded-lg border-2 px-4 py-3 min-w-[220px] max-w-[280px]"
      style={{
        borderColor: selected ? "rgba(255,255,255,0.45)" : color.border,
        background: color.bg,
        boxShadow: selected ? "0 0 0 2px rgba(255,255,255,0.15)" : "none",
      }}
    >
      <Handle type="target" position={Position.Left}
              className="!w-1.5 !h-1.5"
              style={{ background: color.border }} />
      <Handle type="source" position={Position.Right}
              className="!w-1.5 !h-1.5"
              style={{ background: color.border }} />

      <div className="flex items-center justify-between mb-1">
        <span className="text-[9px] uppercase tracking-[0.18em] opacity-60">
          Layer
        </span>
        {data.complexity && (
          <span className="text-[9px] uppercase tracking-wider opacity-60">
            {COMPLEXITY_LABEL[data.complexity] ?? data.complexity}
          </span>
        )}
      </div>
      <div className="font-serif text-base tracking-tight leading-tight">
        {data.name}
      </div>
      {data.description && (
        <p className="text-[11px] opacity-70 mt-1 leading-snug line-clamp-2">
          {data.description}
        </p>
      )}
      <div className="text-[10px] opacity-60 mt-2 inline-flex items-center gap-1">
        <span className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ background: color.dot }} />
        {data.file_count ?? 0} file{data.file_count === 1 ? "" : "s"}
      </div>
    </div>
  );
}

export default memo(PrismLayerNode);
