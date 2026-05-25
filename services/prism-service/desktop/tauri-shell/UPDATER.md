# PRISM Tauri shell — auto-updater setup

The shell ships with Tauri's bundled updater plugin. On launch it
fetches a signed manifest from GitHub Releases, verifies the signature
with the pubkey embedded in the binary, and (when a newer version is
available) shows a native dialog asking the user to install.

## One-time setup before the first signed release

The release workflow needs the minisign private key as a repo secret
to sign each bundle. The matching public key is already embedded in
`src-tauri/tauri.conf.json` (`plugins.updater.pubkey`).

Private key generated 2026-05-24 lives at
`C:\Users\siege\.claude\jobs\eeadb7d2\prism-tauri-signing.key` on the
machine that ran `tauri signer generate`. Move it somewhere safe and
add it to repo secrets:

```bash
# Push the private key contents to GitHub as a repo secret
gh secret set TAURI_SIGNING_PRIVATE_KEY \
    --body "$(cat C:/Users/siege/.claude/jobs/eeadb7d2/prism-tauri-signing.key)" \
    --repo siegeon/.prism

# The keypair was generated WITHOUT a password. If you regenerate
# with one, also set:
# gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD --body "<password>"
```

After that, every `git push origin v5.3.X` triggers
`.github/workflows/release-tauri.yml` — it builds + signs platform
bundles, uploads them to the GitHub Release for that tag, and writes
the `latest.json` manifest that running shells fetch.

## What the updater does NOT cover

- **OS code signing** (Apple Developer ID for macOS, EV cert for
  Windows). Without those, first install shows a SmartScreen /
  Gatekeeper warning. Updates still work once installed because
  Tauri's own minisign verification doesn't need OS signing.
- **Linux AppImage signing** beyond the minisign bundle.

Plumbing those is v6.0.x work — separate certs, separate CI flow.

## Verifying it works end-to-end

1. `gh secret set TAURI_SIGNING_PRIVATE_KEY ...` (above).
2. Bump `PRISM_VERSION` in `services/prism-service/prism_service/__version__.py`.
3. Commit + push to a branch + merge to main (or push directly if
   you're testing — the workflow runs on tags, not branches).
4. `git tag v5.3.X && git push origin v5.3.X`.
5. Watch the Actions tab — `Publish Tauri shell + updater manifest`
   should run on each matrix entry.
6. After ~10 min, https://github.com/siegeon/.prism/releases/latest
   has the bundles + `latest.json`.
7. Launch an OLDER prism-shell.exe — within a second of startup,
   the updater dialog appears: "PRISM 5.3.X available, install now?"

## Recovery if the private key is lost

Generate a new keypair (`npx @tauri-apps/cli signer generate --ci -w
<path>`), update `pubkey` in `tauri.conf.json`, rotate the repo
secret, ship a new release. **Existing installs of versions signed
with the old key can no longer auto-update — users must install the
new version manually once, then auto-update resumes.**
