// Sign in to a PRISM running on another machine (task 4367c12f).
//
// Owner, 2026-07-30: "i tried to log into the endpoint from another machine -
// and i have no way to do so?" ... "i also have the key on the second machine
// that should be enough to access it". It is enough now: this is where it goes.
//
// This screen only ever appears across the network. On the machine running
// PRISM the server trusts loopback, so nobody is asked for anything.
import { useState } from "react";
import { setAccessKey } from "@/lib/auth";
import { api, ApiError } from "@/lib/api";

export default function KeyGatePage({ onAuthed }: { onAuthed: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function connect(e: React.FormEvent) {
    e.preventDefault();
    if (!key.trim() || busy) return;
    setBusy(true);
    setError("");
    // Store, then prove it against a protected route. A key that does not
    // work must not be left behind pretending it does.
    setAccessKey(key);
    try {
      await api.get("/api/auth/me");
      onAuthed();
    } catch (err) {
      setAccessKey("");
      setError(
        err instanceof ApiError && err.status === 401
          ? "That key was not accepted. Copy it again from Settings on the machine running PRISM, or rotate it there."
          : "Could not reach this PRISM. Check the address and that the machine is awake.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full w-full grid place-items-center bg-[color:var(--background-base)] p-6">
      <form onSubmit={connect} className="w-full max-w-md flex flex-col gap-4">
        <div>
          <h1 className="text-lg font-semibold" style={{ color: "var(--text-primary)" }}>
            Sign in to this PRISM
          </h1>
          <p className="text-sm mt-1.5 leading-relaxed" style={{ color: "var(--text-secondary)" }}>
            You are opening PRISM from another machine, so it needs your access
            key. Open Settings on the machine running PRISM and copy the key
            there.
          </p>
        </div>

        <label className="flex flex-col gap-1.5">
          <span className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            Access key
          </span>
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            autoFocus
            spellCheck={false}
            autoComplete="off"
            placeholder="paste your access key"
            className="px-3 py-2 rounded-md font-mono text-sm border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
            style={{ color: "var(--text-primary)" }}
          />
        </label>

        {error && (
          <div
            className="text-sm px-3 py-2 rounded-md leading-relaxed"
            style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)" }}
            role="alert"
          >
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !key.trim()}
          className="px-3 py-2 rounded-md text-sm font-semibold disabled:opacity-50"
          style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
        >
          {busy ? "Checking…" : "Connect"}
        </button>

        <p className="text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
          Anyone with this key can read and change everything in this PRISM.
          If it leaks, rotate it in Settings on the machine running PRISM.
        </p>
      </form>
    </div>
  );
}
