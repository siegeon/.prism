/**
 * useRoles — the SPA-side reader for the single source of truth on agent roles.
 *
 * `GET /api/roles` returns the canonical registry: exactly three roles
 * (sm=Steward/frontier, qa=Verifier/balanced, dev=Builder/fast), the tier
 * descriptions, the effort ladder, and the step→role assignment map. Every
 * surface that names a role (the per-role token ledger on /learning, the
 * conductor StepRail) joins to this registry instead of hardcoding labels.
 *
 * The fetch is fired once per app session and memoised at module scope so the
 * many components that ask for it share one request. Fetch failures (e.g. the
 * backend lane hasn't landed the route yet → 404) resolve to an EMPTY registry
 * so callers degrade to the raw role id rather than throwing.
 */
import { useEffect, useState } from "react";
import { fetchJSON } from "./api";

export type RoleMeta = {
  id: string;
  label: string;
  tier: string;
  effort: string;
  purpose?: string;
  tier_desc?: string;
};

export type RoleRegistry = {
  tiers: Record<string, string>;
  efforts: string[];
  roles: Record<string, RoleMeta>;
  step_roles: Record<string, string>;
};

const EMPTY: RoleRegistry = { tiers: {}, efforts: [], roles: {}, step_roles: {} };

// Module-scoped memo: one in-flight promise shared by every hook consumer.
let cache: RoleRegistry | null = null;
let inflight: Promise<RoleRegistry> | null = null;

function normalize(raw: Partial<RoleRegistry> | null | undefined): RoleRegistry {
  if (!raw || typeof raw !== "object") return EMPTY;
  return {
    tiers: raw.tiers ?? {},
    efforts: Array.isArray(raw.efforts) ? raw.efforts : [],
    roles: raw.roles ?? {},
    step_roles: raw.step_roles ?? {},
  };
}

export function loadRoles(): Promise<RoleRegistry> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = fetchJSON<Partial<RoleRegistry>>("/api/roles")
      .then((d) => {
        cache = normalize(d);
        return cache;
      })
      .catch(() => {
        // 404 / network → empty registry; callers fall back to raw ids.
        cache = EMPTY;
        return cache;
      });
  }
  return inflight;
}

export type UseRoles = {
  registry: RoleRegistry;
  loading: boolean;
  /** Role id that owns a workflow step (from step_roles), or undefined. */
  roleFor: (step: string | undefined | null) => string | undefined;
  /** Full role metadata for a role id, or undefined if not in the registry. */
  roleMeta: (roleId: string | undefined | null) => RoleMeta | undefined;
};

export function useRoles(): UseRoles {
  const [registry, setRegistry] = useState<RoleRegistry>(cache ?? EMPTY);
  const [loading, setLoading] = useState(!cache);

  useEffect(() => {
    let alive = true;
    loadRoles().then((r) => {
      if (!alive) return;
      setRegistry(r);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  return {
    registry,
    loading,
    roleFor: (step) => (step ? registry.step_roles[step] : undefined),
    roleMeta: (roleId) => (roleId ? registry.roles[roleId] : undefined),
  };
}
