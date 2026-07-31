import { useState } from "react";
import { api } from "@/lib/api";
import Backdrop from "@/components/Backdrop";
import ConnectExistingPrism from "@/components/ConnectExistingPrism";

type Claimed = { key: string; id: string; label: string; created_at: string };

/**
 * The one-time claim screen (task fa52ba9e, decision mx-935cc2 / mx-30fc0c).
 * Shown by App only when the instance is UNCLAIMED — i.e. the first time PRISM
 * opens after auto-updating from a credential-free build. The existing owner
 * names themselves once (no setup code, no db edit) and is handed their key.
 * Additive: claiming never touches existing tasks or memory.
 */
export default function ClaimPage({ onClaimed }: { onClaimed: () => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [key, setKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const canClaim = name.trim().length > 0 && email.trim().length > 0 && !busy;

  const claim = async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.post<Claimed>("/api/auth/claim", { name, email });
      setKey(res.key);
    } catch (e) {
      setErr(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  const copy = async (t: string) => {
    try { await navigator.clipboard.writeText(t); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { /* still visible to copy by hand */ }
  };

  return (
    <div className="h-full w-full flex items-center justify-center overflow-y-auto bg-[color:var(--background-base)] text-[color:var(--midground-base)] relative">
      <Backdrop />
      <div className="relative z-10 w-full max-w-[520px] px-6 py-12">
        {key === null ? (
          <>
            <h1 className="text-[34px] leading-[1.15] font-extrabold tracking-tight mb-3">
              Welcome back. Claim<br />this PRISM as{" "}
              <span className="bg-gradient-to-r from-[#60a5fa] to-[#a78bfa] bg-clip-text text-transparent">yours</span>.
            </h1>
            <p className="text-[15px] leading-relaxed text-[color:var(--text-secondary)] mb-7 max-w-[44ch]">
              PRISM just updated itself. This instance is running without an owner, so before anyone else can reach it, we confirm it's you. This happens once. No setup code, nothing to edit.
            </p>

            <label className="block mb-5">
              <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mb-2">Your name</div>
              <input
                value={name} onChange={(e) => setName(e.target.value)} autoFocus
                className="w-full rounded-[10px] border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/50 px-4 py-3 text-[15px] outline-none focus:border-[color:var(--accent-teal-fg)]"
              />
            </label>
            <label className="block mb-5">
              <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mb-2">
                Email <span className="normal-case tracking-normal opacity-80">· how teammates will see you</span>
              </div>
              <input
                value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@yourdomain.dev" type="email"
                className="w-full rounded-[10px] border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/50 px-4 py-3 text-[15px] outline-none focus:border-[color:var(--accent-teal-fg)]"
              />
            </label>

            {err && (
              <div className="mb-4 rounded-[10px] border border-[color:var(--accent-rose-fg)]/40 bg-[color:var(--accent-rose-fg)]/10 px-4 py-3 text-[13px]">{err}</div>
            )}

            <div className="flex items-center gap-4 mb-1">
              <button
                onClick={claim} disabled={!canClaim}
                className="inline-flex items-center gap-2 rounded-[10px] px-5 py-3 text-[14.5px] font-bold text-[#0a0f1e] bg-gradient-to-r from-[#7c9cff] to-[#b48bff] shadow-[0_6px_22px_-8px_#7c6bff] disabled:opacity-50"
              >
                {busy ? "Claiming…" : "Claim this instance →"}
              </button>
              <span className="text-[12.5px] text-[color:var(--text-muted)] max-w-[20ch]">You won't be asked to sign in again on your own instance.</span>
            </div>

            <hr className="my-7 border-[color:var(--border-default)]" />

            <ConnectExistingPrism />

            <hr className="my-7 border-[color:var(--border-default)]" />

            <div className="flex items-center gap-2 font-bold text-[15px] mb-3"><span className="text-[color:var(--accent-emerald-fg)]">✓</span> Your data is untouched</div>
            <p className="text-[14px] leading-relaxed text-[color:var(--text-secondary)]">
              Your tasks, memories, projects and history are exactly as you left them. Claiming only records who the owner is. It never resets anything.
            </p>

            <div className="mt-5 flex gap-3 items-start rounded-[10px] border border-[color:var(--accent-violet-fg)]/40 bg-[color:var(--accent-violet-fg)]/10 px-4 py-3 text-[13px] leading-relaxed text-[color:var(--text-secondary)]">
              <span className="text-[color:var(--accent-violet-fg)] shrink-0">🛡</span>
              <div><b className="text-[color:var(--text-primary)]">Until you claim, PRISM answers no one.</b> After the update lands and before you claim, it serves only this screen, so nothing on your network is reachable and there's no window for a stranger to grab your instance first.</div>
            </div>
          </>
        ) : (
          <>
            <h1 className="text-[28px] font-extrabold tracking-tight mb-2">You're the owner.</h1>
            <p className="text-[15px] leading-relaxed text-[color:var(--text-secondary)] mb-6 max-w-[44ch]">
              Here is your access key. It's how agents and MCP reach PRISM, and how you invite people. You can always read it again in Settings → Access key.
            </p>
            <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mb-2">Access key</div>
            <div className="flex items-center gap-2 mb-6">
              <code className="flex-1 min-w-0 truncate rounded-[10px] border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/50 px-4 py-3 font-mono text-[13px] text-[color:var(--accent-teal-fg)]">{key}</code>
              <button onClick={() => copy(key)} className="shrink-0 rounded-[10px] border border-[color:var(--border-default)] px-4 py-3 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10">{copied ? "Copied" : "Copy"}</button>
            </div>
            <button onClick={onClaimed} className="inline-flex items-center gap-2 rounded-[10px] px-5 py-3 text-[14.5px] font-bold text-[#0a0f1e] bg-gradient-to-r from-[#7c9cff] to-[#b48bff] shadow-[0_6px_22px_-8px_#7c6bff]">
              Continue into PRISM →
            </button>
          </>
        )}
      </div>
    </div>
  );
}
