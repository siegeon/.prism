---
name: magic-build
description: Build a customer's business backend on a headless Magic Cloud tenant through PRISM's /api/magic channel — CRUD + business-rule validation in Hyperlambda. Use when a task builds LOB functionality (tables + endpoints) on Magic. Encodes the Hyperlambda gotchas so you never rediscover them.
version: 1.0.0
---

# Build on Magic Cloud (headless, via PRISM)

You build a customer's backend on a Magic tenant by POSTing Hyperlambda to
PRISM's channel. You never edit PRISM or Magic source. This skill gives you
COPY-PASTE templates — change only the db/table/field names. Do NOT invent
Hyperlambda from scratch; adapt these.

## Channel
- `POST http://localhost:8888/api/magic/execute` body `{"hyperlambda":"<code>"}` → runs Hyperlambda as root, returns `{"result": <raw text>}`. Your build + admin tool. (Dev daemon is :8888; a scratch daemon may be :8999.)
- `GET http://localhost:8888/api/magic/endpoints` → lists live endpoints (verify your work here).
- Live tenant endpoints answer at `http://localhost:4444/magic/modules/<mod>/<name>`.

## THE FIVE GOTCHAS (each cost millions of tokens to rediscover — obey them)

1. **`io.file.save` does NOT create parent folders.** Always `io.folder.create:/modules/<mod>/` FIRST, or you get `DirectoryNotFoundException`.
2. **Node references resolve only among ELDER SIBLINGS + ancestors — NOT a sibling's children.** So `x:@sqlite.select/*` works ONLY when the referencing node is a DIRECT SIBLING of `sqlite.select` INSIDE the same `sqlite.connect` block. A root-level `return-nodes:x:@sqlite.select/*` silently returns an EMPTY body, and a root-level `if exists:x:@sqlite.select/*` reads empty → falsely rejects everything. FIX: put your return / validation checks INSIDE the connect block, indented as siblings of the select.
3. **Indentation IS control flow.** Statements must be siblings at equal indent. A node accidentally nested under another runs as its argument (silent no-op).
4. **A write (`sqlite.execute` INSERT/UPDATE) nested inside an `else > .lambda` branch can silently no-op in the live endpoint runtime.** Prefer a flat guard-then-write: validate, `return` early on failure, then do the write at top level. Don't bury the write in a deep branch.
5. **`/api/magic/execute` echoes the whole lambda tree as raw text — it is NOT proof a branch ran.** Verify side effects with a follow-up `sqlite.select`, not by reading the echo. (`return-nodes` through the execute channel may 500; that's fine — it works in the actual endpoint file.)

## TEMPLATE A — create schema + seed (one execute call)
```
sqlite.connect:DBNAME
   sqlite.execute:CREATE TABLE IF NOT EXISTS parents (id INTEGER PRIMARY KEY, name TEXT, email TEXT)
   sqlite.execute:CREATE TABLE IF NOT EXISTS children (id INTEGER PRIMARY KEY, parent_id INTEGER, title TEXT, status TEXT DEFAULT 'new')
   sqlite.execute:INSERT INTO parents (name,email) VALUES ('Seed Co','seed@x.io')
```
Keep DDL/INSERT SQL on ONE line, unquoted, to avoid nested-quote escaping.

## TEMPLATE B — list endpoint (GET). Note return-nodes is INSIDE connect (gotcha #2)
```
io.folder.create:/modules/MODNAME/
io.file.save:/modules/MODNAME/parents.get.hl
   .:@"sqlite.connect:DBNAME
   sqlite.select:SELECT * FROM parents
   return-nodes:x:@sqlite.select/*"
```
Live at `GET magic/modules/MODNAME/parents`.

## TEMPLATE C — add endpoint with a foreign-key + rule (POST). VERIFIED idiom.
Guard-then-write (gotcha #4); the select + if-guards are siblings INSIDE connect (gotcha #2).
```
io.file.save:/modules/MODNAME/children.post.hl
   .:@".arguments
   parent_id:long
   title:string
sqlite.connect:DBNAME
   sqlite.select:SELECT id FROM parents WHERE id = @id
      @id:x:@.arguments/*/parent_id
   if
      not
         exists:x:@sqlite.select/*
      .lambda
         response.status.set:400
         return
            error:R1 - no such parent
   sqlite.execute:INSERT INTO children (parent_id,title) VALUES (@p,@t)
      @p:x:@.arguments/*/parent_id
      @t:x:@.arguments/*/title
   return
      status:created"
```
VERIFIED RULES (use these exact slots — do NOT invent validators):
- **Read a POST arg:** `x:@.arguments/*/<name>`. Declare typed args in a top `.arguments` block.
- **SQL params use `@name`** (both in the SQL text `= @id` AND the child node `@id:x:...`). NOT `:name`.
- **FK / "must exist" reject:** `if / not / exists:x:@sqlite.select/*` after a select that looks the row up.
- **"already exists" / uniqueness reject:** `if / exists:x:@sqlite.select/*` (no `not`).
- **Count / capacity rule (e.g. can't exceed N):** fold it into the lookup SQL and reject if empty —
  `SELECT id FROM events WHERE id = @id AND capacity > (SELECT COUNT(*) FROM registrations WHERE event_id = @id)` then `if / not / exists` → this rejects BOTH a missing event (R1) and a full one (R2) in one check.
- **Enum rule (status in a set):** guard in SQL or with `if` on the arg value; simplest is to only ever write a fixed value (add writes 'pending', a separate approve writes 'approved') so an invalid status cannot enter.
- **Numeric rule (amount>0):** `if / lt / (arg) / .:int:0 / .lambda → 400`. Keep the comparison a sibling inside connect.

## TEMPLATE D — an "approve"/update endpoint
```
io.file.save:/modules/MODNAME/children.approve.post.hl
   .:@"sqlite.connect:DBNAME
   sqlite.execute:UPDATE children SET status='approved' WHERE id = @id
      @id:x:@.arguments/*/id
   return
      status:approved"
```

## Recipe (do in this order)
1. Schema + seed via Template A (one call).
2. For each entity: a GET list (Template B) and a POST add (Template C with its rules). Folder-create once.
3. Verify: `GET /api/magic/endpoints` shows your `magic/modules/MODNAME/*`; then probe each rule with a violating input and confirm a 400, plus a happy-path 200.
4. If a probe wrongly succeeds/fails, the cause is almost always gotcha #2 (a check at the wrong indent) — move it inside the connect block.

## When to prefer crudify
For plain CRUD with NO custom business rules, `POST magic/system/crudifier/crudify` (via the client, non-AI, deterministic) generates all four verbs from a table in one shot. Use custom `.hl` (templates above) whenever a rule must be enforced in the endpoint. Never use the AI/ML crudify options or `magic.lambda.openai` slots — a headless tenant has no model key.
