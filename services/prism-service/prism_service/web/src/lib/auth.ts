// The access key this browser uses to reach a PRISM on another machine.
//
// On the machine running PRISM there is no key and never was: the server
// trusts loopback, so nothing here is set and every request goes out bare.
// It only fills in when you open PRISM across the network, where the server
// asks for the key it already showed you in Settings (task 4367c12f).
//
// localStorage, not a cookie: the key is a bearer credential and must never
// ride along automatically on a cross-site request.
const KEY = "prism.accessKey";

export function accessKey(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";   // private-mode / storage disabled — behave as "no key"
  }
}

export function setAccessKey(secret: string): void {
  try {
    const trimmed = secret.trim();
    if (trimmed) localStorage.setItem(KEY, trimmed);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage disabled: the key lives for this page load only */
  }
}

export function clearAccessKey(): void {
  setAccessKey("");
}

export function authHeaders(): Record<string, string> {
  const secret = accessKey();
  return secret ? { authorization: `Bearer ${secret}` } : {};
}
