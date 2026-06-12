---
name: mermaid-syntax
description: Author valid Mermaid diagram source for PRISM plan_diagram fields and plan documents. Use when synthesizing a plan_diagram (prototype/implement workflows), when a task needs a sequence/class/state/ER/flowchart/C4 diagram, or when a Mermaid diagram fails to parse in PlanView. Covers the 6 core diagram types, C4 layout science, and Hermes theming.
license: MIT
attribution: >
  Trimmed port of syntax reference material from the mermaid-js/mermaid
  documentation (MIT License, Copyright (c) 2014-2024 Knut Sveidqvist)
  reshaped for PRISM's plan_diagram pipeline. arc-kit governance skill
  structure (MIT) used as the template.
---

# Mermaid syntax — the 6 diagram types PRISM renders

PRISM renders `plan_diagram` via mermaid in `PlanView.tsx`. The FIRST
non-empty line MUST be a diagram-type keyword — prose first lines fail
the plan_coverage rubric (`arc_governance.mermaid_parses`). Emit RAW
mermaid source: no code fences, no leading commentary.

## 1. flowchart — structure & layer dependencies

```
flowchart TD
  api --> services
  services --> models
  services -.-> brain[(brain.db)]
```

- `TD` top-down, `LR` left-right. `A --> B` directed edge; `-.->` dashed
  (async/optional); `==>` thick (hot path).
- Layer-edge diagrams feed the governance conformance check: an edge
  `domain --> infrastructure` is diffed against Brain-stored principles.
  Draw REAL intended edges only.

## 2. sequenceDiagram — actor ↔ system journeys

```
sequenceDiagram
  participant U as User
  participant S as prism-service
  U->>S: POST /api/conductor/gate
  S-->>U: {ok, gate_state}
```

- `->>` solid call, `-->>` dashed reply, `Note over A,B: text`.
- Best default for PRD journeys (who calls what, in what order).

## 3. classDiagram — types & relationships

```
classDiagram
  ConductorService --> TaskService : advances
  ConductorService ..> ArcGovernance : scores rubrics
  class ConductorService {
    +gate_decide(task_id, action)
  }
```

## 4. stateDiagram — lifecycle machines

```
stateDiagram-v2
  [*] --> pending
  pending --> passed : gate_decide(approve)
  pending --> failed : verifier rejects
  failed --> passed : override (distinct actor)
```

Use for gate/task lifecycle changes (the conductor IS a state machine).

## 5. erDiagram — data shapes

```
erDiagram
  TASK ||--o{ TASK_SESSION : links
  TASK { string id PK string workflow_step string gate_state }
```

## 6. journey / gantt — phased delivery

```
gantt
  title Phase plan
  section Build
  red tests   :a1, 2026-06-01, 1d
  implement   :after a1, 2d
```

# C4 layout science (c4-layout-science)

When a diagram describes SYSTEM CONTEXT rather than code, use C4 levels
(`C4Context` / `C4Container` / `C4Component` keywords are supported):

1. **One level per diagram** — never mix context and component boxes.
2. **Max ~9 boxes** — beyond that, split into a second diagram.
3. **Dependencies point downward** (toward infrastructure); an upward
   arrow is a smell the governance principles will flag.
4. **Label edges with verbs** ("reads", "publishes"), not nouns.

# Hermes theming

PlanView renders mermaid with PRISM's Hermes identity — do NOT inline
`%%{init: {"theme": ...}}%%` overrides or per-node `style`/`fill`
directives; the app applies Hermes tokens (muted backgrounds, amber
accents, serif titles) globally. Keep diagrams structural; let Hermes
carry the look. Exception: `classDef` for a semantic group is fine if
it carries no hard-coded colors.

# Checklist before emitting plan_diagram

- [ ] First line is a bare diagram keyword (e.g. `flowchart TD`).
- [ ] No code fences, no prose above the keyword.
- [ ] Layer edges reflect INTENDED architecture (rubric diffs them
      against Brain principles).
- [ ] Node ids are plain identifiers (`api`, `services`), labels in
      brackets when prose is needed.
