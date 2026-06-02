// Shared motion vocabulary for the PRISM SPA (task a5e0d9f5).
//
// Motion as STRUCTURE, not decoration: calm, purposeful, GPU-only
// (transform/opacity), color exclusively via --accent-*/--surface-*/--text-*
// tokens. One vocabulary so every surface tweens with the same feel.
//
// Zero new dependencies — these are plain objects consumed by motion@12
// (imported from "motion/react" at the call sites).

import type { Transition, Variants } from "motion/react";

// Cubic-bezier easings. EASE_OUT (expo-out) decelerates INTO place — the
// default for anything entering. EASE_IN accelerates OUT — for exits.
export const EASE_OUT = [0.16, 1, 0.3, 1] as const;
export const EASE_IN = [0.4, 0, 1, 1] as const;

// Durations (seconds). Exits are FASTER than entrances so leaving feels
// crisp and arriving feels settled.
export const DUR = { enter: 0.24, exit: 0.16, chip: 0.2 } as const;

// Critically-damped spring (bounce < 0.1) for layout / reorder FLIP — no
// wobble, which would read as "toy". Used for board card reorder + the
// child-task checkbox tick.
export const SPRING_SNAPPY: Transition = {
  type: "spring",
  stiffness: 460,
  damping: 38,
  mass: 1,
};

// Stagger cap: 0.04s per item, capped at 12 so a long column never exceeds
// ~0.5s of cascade.
export function staggerDelay(i: number): number {
  return Math.min(i, 12) * 0.04;
}

// Card-enter variants for stacked Card stagger on a page mount. The reduced
// variant collapses to opacity-only (no translate) so reduced-motion users
// still get a gentle fade without travel.
export function cardEnter(i: number, reduced: boolean): Variants {
  return {
    initial: reduced ? { opacity: 0 } : { opacity: 0, y: 8 },
    animate: {
      opacity: 1,
      y: 0,
      transition: {
        duration: reduced ? 0 : DUR.enter,
        ease: EASE_OUT,
        delay: reduced ? 0 : staggerDelay(i),
      },
    },
  };
}
