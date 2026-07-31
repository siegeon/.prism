import { useState } from "react";

/**
 * The SECOND door on a fresh install (task b064db4e).
 *
 * A brand-new machine offered exactly one path, "Claim this PRISM as yours",
 * which invited the owner to claim a second, empty instance rather than reach
 * the one holding their work. This control is the missing half: point this
 * browser at the PRISM you already run.
 *
 * It deliberately holds NO credential. Remote sign-in already shipped (task
 * 4367c12f): the target instance serves its own sign-in screen and stores and
 * proves the key there. This control's entire job is to navigate, so that
 * discovery adds no second credential path and widens no exposure.
 */
export default function ConnectExistingPrism({ compact = false }: { compact?: boolean }) {
  const [addr, setAddr] = useState("");
  const [err, setErr] = useState<string | null>(null);

  /** Accept what a person actually types: "192.168.1.20:8888", "prism.box",
   *  or a full url. Returns null when there is nothing usable to go to. */
  const normalize = (raw: string): string | null => {
    const typed = raw.trim().replace(/\/+$/, "");
    if (!typed) return null;
    const withScheme = /^https?:\/\//i.test(typed) ? typed : `http://${typed}`;
    try {
      const u = new URL(withScheme);
      return u.host ? u.toString().replace(/\/+$/, "") : null;
    } catch {
      return null;
    }
  };

  const connect = () => {
    const target = normalize(addr);
    if (!target) {
      setErr("Enter the address of your PRISM, for example 192.168.1.20:8888");
      return;
    }
    window.location.assign(target);
  };

  return (
    <div className={compact ? "mt-4" : "mt-1"}>
      <div className="flex items-center gap-2 font-bold text-[15px] mb-2">
        <span className="text-[color:var(--accent-teal-fg)]">↗</span> Already run PRISM somewhere else?
      </div>
      <p className="text-[14px] leading-relaxed text-[color:var(--text-secondary)] mb-3 max-w-[46ch]">
        Connect to your existing PRISM instead of claiming this one. Enter its address, then sign in there with your access key. This instance is left exactly as it is, and nothing is copied between them.
      </p>
      <div className="flex items-center gap-2">
        <input
          value={addr}
          onChange={(e) => { setAddr(e.target.value); setErr(null); }}
          onKeyDown={(e) => { if (e.key === "Enter") connect(); }}
          placeholder="192.168.1.20:8888"
          aria-label="Address of your existing PRISM"
          className="flex-1 min-w-0 rounded-[10px] border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/50 px-4 py-3 text-[15px] outline-none focus:border-[color:var(--accent-teal-fg)]"
        />
        <button
          type="button"
          onClick={connect}
          className="shrink-0 rounded-[10px] border border-[color:var(--border-default)] px-5 py-3 text-[14.5px] font-bold hover:bg-[color:var(--midground-base)]/10"
        >
          Connect →
        </button>
      </div>
      {err && (
        <div className="mt-2 text-[13px] text-[color:var(--accent-rose-fg)]">{err}</div>
      )}
    </div>
  );
}
