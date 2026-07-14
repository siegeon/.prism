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
import { ACCENT_HEX, hexToRgba } from "@/lib/palette";

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

// Layer palette — v6.0.24 refresh. Sourced from src/lib/palette.ts so
// it stays in lock-step with the --accent-{tone}-fg tokens used by
// /memory chips and the /graph community palette. Border + bg derive
// from the same hex at fixed alphas (0.55 / 0.10), matching the old
// hand-authored triples but without the per-color rgba literal drift.
const PALETTE = ACCENT_HEX.map((hex) => ({
  border: hexToRgba(hex, 0.55),
  bg: hexToRgba(hex, 0.10),
  dot: hex,
}));
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
        <span className="text-2xs uppercase tracking-[0.18em] opacity-60">
          Layer
        </span>
        {data.complexity && (
          <span className="text-2xs uppercase tracking-wider opacity-60">
            {COMPLEXITY_LABEL[data.complexity] ?? data.complexity}
          </span>
        )}
      </div>
      <div className="font-serif text-base tracking-tight leading-tight">
        {data.name}
      </div>
      {data.description && (
        <p className="text-2xs opacity-70 mt-1 leading-snug line-clamp-2">
          {data.description}
        </p>
      )}
      <div className="text-2xs opacity-60 mt-2 inline-flex items-center gap-1">
        <span className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ background: color.dot }} />
        {data.file_count ?? 0} file{data.file_count === 1 ? "" : "s"}
      </div>
    </div>
  );
}

export default memo(PrismLayerNode);
