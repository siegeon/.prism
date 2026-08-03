---
description: Operate the open PR queue to conclusion through PRISM (loop-safe)
argument-hint: "[dry_run] [no-merge] [close-drafts] [max=N]"
---

Run one convergence tick over this repo's open pull requests.

Invoke the workflow by PATH, never by name (a `name:` lookup can resolve a stale
copy of the script):

```
Workflow({
  scriptPath: ".claude/workflows/prs.js",
  args: { api_base: "http://127.0.0.1:8888" }
})
```

Dev on this machine is port 8888; a release instance is 7778. Fold `$ARGUMENTS`
into `args` when present: `dry_run` -> `{dry_run:true}` (survey only, changes
nothing), `no-merge` -> `{merge:false}`, `close-drafts` ->
`{close_stale_drafts:true}`, `max=N` -> `{max_prs:N}`.

Then report the returned digest in your own words, in this order, and keep it to
what changed:

1. What MERGED this tick (PR number, sha, and whether PRISM was reconciled).
2. `awaiting_prism` and `done_but_unmerged` - the drift. For each, name the PR
   and the gate its PRISM task still owes. These are the ones only the owner can
   release; say plainly that the workflow deliberately did not merge them.
3. `halted` - anything that needs a decision, with the decision stated.
4. One line for everything else (advanced / unlinked / deferred / orphan tasks).

A tick with nothing to do returns `mode: "quiet_tick"` - report that in a single
line and stop. Do not re-run the workflow in the same turn, and never merge a PR
by hand that the workflow declined to merge.
