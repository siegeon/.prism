---
name: gauntlet-loop
description: Turns any goal into one short, paste-ready "gauntlet loop" prompt - a prompt that makes an agent set a concrete quality bar, split the work into small judgeable pieces, run a builder and a separate harsh critic on each, compare blind against the bar, and loop until it wins. When nothing shipped exists to compare against, it interviews you into an answer key of binary checks and makes that the bar. Works for builds, writing, code, research, or design. Use whenever the user says "/gauntlet-loop", "gauntlet loop", "gauntlet this", "make a gauntlet prompt", "loop until it beats X", or wants an agent to grind against a real reference until it wins.
---

# Gauntlet Loop

The user gives a goal. You give back ONE short prompt they can paste into a fresh agent session.

You are not doing the work. You are writing the prompt that makes another agent grind on the work until it beats a real reference.

## Flow

1. **Read the goal.** One line restatement in your head, not on screen.
2. **Set the bar.** If the user supplied a reference, use it. If not, offer **2 or 3 candidate bars**, one line each, and stop. Wait for their pick. Do not write the prompt yet. If nothing on offer passes all three tests below, do not invent a bar. Go to **When no bar exists** and build an answer key instead.
3. **Write the prompt.** One block, paste-ready, no preamble, no headings inside it, no narration after it.
4. **Offer to run it.** One flat line under the prompt: "I can run this here." Not a question.

If they say run it, you become the lead agent and follow the prompt you just wrote.

## The bar is the whole trick

Everything else in a gauntlet loop is scaffolding. The loop only produces quality if the thing it compares against is real.

A bar has to pass three tests:

- **Named.** A specific thing, not a category. "Stripe's pricing page" works. "Award-winning SaaS sites" does not.
- **Fetchable.** The critic can actually get it - screenshot the live page, read the published piece, run the binary, open the repo, watch the footage. If the agent cannot obtain it, it will hallucinate the comparison.
- **Comparable.** Both can sit side by side and a judge can pick one. If you cannot imagine the A/B, it is not a bar.

Bars by goal type:

| Goal | Bar that works |
|---|---|
| Website, app, UI | The live site of a specific best-in-class product, screenshotted at the same viewport |
| Game, 3D, visual | Real footage or screenshots from a named shipped title |
| Writing | A specific published piece by a named author or publication, same length and format |
| Code, tooling | A named repo's implementation, plus its benchmark or test suite as the measurable half |
| Research, analysis | A named analyst report or a paper's methods section, judged on rigour and coverage |
| Deck, doc, deliverable | A real artifact from a firm known for it, same page count |

When you propose bars, prefer the hardest one the agent can genuinely reach. A bar that is too easy makes the loop exit on round one.

If the goal has a measurable half (load time, token cost, benchmark score, word count, pass rate), name it alongside the reference. Taste plus a number beats taste alone.

## When no bar exists

Most real work has nothing shipped to compare against. Your own product, your own billing rules, an internal tool. There is no Call of Duty for it.

Do not paper over this with a category ("best-in-class dashboards") or with the agent's own taste. A loop with no bar does not fail loudly. It invents a standard, passes its own work against it, and hands back a pile of finished-looking output measured by nothing.

Build an **answer key** instead. It plays the same role the reference played: something outside the builder that says done or not done.

**Interview first, in rounds.** Ask the whole frontier of open questions in one round, numbered, each with your recommended answer, then wait. The user's answers unblock the next round. Stop when nothing is left silently assumed. Facts you can look up yourself are your job, never a question for the user. Decisions are theirs.

Cover at minimum: who this is for, what is in and what is explicitly out, what done looks like on screen, what goes wrong once people use it.

**Then write the key.** One file. Nothing but checks, every line binary:

```
- [ ] A first-time user reaches <the thing> in one click from the home screen.
- [ ] Submitting an empty form shows an inline error, not a toast.
- [ ] The list renders 10k rows without a visible stall.
```

Three rules on the key:

- **Headline outcomes first.** The opening lines are the user's own words about what they will look at when they judge this. Those lines are the loop's report card. Progress gets graded against them, never against work completed.
- **Every line observable.** A check a person can watch pass or fail. "Uses a clean architecture" is not a check. If you cannot say who would see it and where, cut it.
- **No line the builder can rewrite.** The key is written before the build and the builders do not edit it. If a check turns out wrong, that goes back to the user, not into the key.

## Prompt template

Adapt the wording every time. Fill the brackets, keep it short, keep the last line.

```
Build [GOAL].

The bar is [BAR]. Get the real thing first and compare against it directly, not against a description of it.

Break this into the smallest pieces that can be improved and judged on their own. For each piece, fan out a builder and a separate critic with fresh context. The critic inspects the actual output, puts it next to the bar blind with the labels stripped, says which one is better, and names the single biggest remaining gap. Then it goes back to the builder.

The critic should be a harsh critic. Praise is not useful. If ours does not win, it keeps going.

/loop on each piece until the critic picks ours blind. Do not stop before that.

Keep a live progress page updating as the work evolves so I can watch it.

Fan out subagents and ultracode.
```

Rules for what you fill in:

- Bake the bar in as a concrete, fetchable thing. URL, product name, repo, title.
- Add a budget or cost ceiling line **only if the user named one**. No default cap.
- Add tool names only if the goal needs them (image or video generation, a browser, a deploy target).
- Everything else stays out. No architecture, no file layout, no decomposition, no round count, no stack choice unless the user demanded it. The agent decides those, and it decides better than a spec written before the work started.

### Answer-key variant

When the bar is a key rather than a reference, two lines change and the rest of the template stands.

```
The bar is [PATH TO THE KEY]. Every line in it is pass or fail. No piece is done until every line that touches it passes. Do not edit the key. If a check is wrong, stop and tell me.

The first [N] lines are what I will actually look at. Report against those lines, not against work completed, and tell me the moment one of them is blocked on a decision only I can make.
```

The critic still runs separately with fresh context, still gets a binary job, still never builds. It reads the checks instead of a screenshot of somebody else's product.

## Length and voice

Short. Around 120 to 180 words. If the prompt needs a heading to stay readable, it is too long.

Plain sentences. No bullet lists inside the prompt. It should read like someone telling an agent what perfect looks like and refusing to accept less.

## Portability

`/loop` and `ultracode` are Claude Code features. `/loop` reruns the prompt on an interval or lets the model pace itself. `ultracode` opts the turn into multi-agent orchestration.

For any other agent, swap the last two lines for: "Keep looping until the critic picks ours. Run the builders and critics as parallel subagents." The structure carries over unchanged.

## Two filled examples

**Visual goal.** User: "landing page for my running brand, athletic, green and dark, has to feel alive."

Bars offered: A) Nike's current running campaign page B) On Running's homepage C) Gymshark's product landing page. User picks A.

```
Build a landing page for a running brand. Athletic, peak performance, green and dark, energetic, aimed at a young healthy audience. It needs to be interactive and visually unmistakable.

The bar is Nike's current running campaign page. Screenshot it at desktop and mobile and compare against those directly, not against a description of them.

Break this into the smallest pieces that can be improved and judged on their own - hero, motion, type, colour, imagery, interaction, mobile. For each piece, fan out a builder and a separate critic with fresh context. The critic opens the real page in a browser, puts our screenshot next to Nike's blind with the labels stripped, says which is better, and names the single biggest remaining gap. Then it goes back to the builder.

The critic should be a harsh critic. Praise is not useful. If ours does not win, it keeps going.

/loop on each piece until the critic picks ours blind. Do not stop before that.

Keep a live progress page updating as the work evolves so I can watch it.

Fan out subagents and ultracode.
```

**Non-visual goal.** User: "a 2000-word explainer on vector databases for non-engineers."

Bars offered: A) a specific Stripe engineering blog explainer B) a named Julia Evans post C) the Wikipedia article plus a comprehension test. User picks B.

```
Write a 2000-word explainer on vector databases for readers who are smart but not engineers.

The bar is Julia Evans' writing on hard technical topics. Pull three of her actual posts and compare against them directly, not against a description of her style.

Break this into the smallest pieces that can be judged on their own - the opening, each explanation, the diagrams, the analogies, the ending. For each piece, fan out a writer and a separate critic with fresh context. The critic reads ours and hers blind with the bylines stripped, says which one a non-engineer would understand faster, and names the single biggest remaining gap. Then it goes back to the writer.

The critic should be a harsh critic. Praise is not useful. If ours does not win, it keeps going.

/loop on each piece until the critic picks ours blind. Do not stop before that.

Keep a live progress page updating as the work evolves so I can watch it.

Fan out subagents and ultracode.
```

## What breaks a gauntlet loop

- **A vague bar.** The critic invents a comparison and approves everything. Most common failure by far.
- **No bar at all, dressed as one.** The goal had nothing shipped to compare against and the loop ran anyway. Every round passes, the output looks finished, and it was measured by nothing. Write an answer key instead.
- **Grading the work instead of the outcome.** The loop reports rounds run, pieces closed, changes shipped. The user judges by the two or three things they said they would look at. A run can close everything on its list and still be a failure if those things never visibly landed. Grade the headline lines out loud, every report.
- **A headline check parked quietly.** One of the top lines is blocked on a decision or a permission only the user can give, and the loop keeps grinding the rest and mentions it in passing. That blocked line is the whole status message until it moves.
- **The builder judging its own work.** The critic must be a separate agent with fresh context. It should not know how hard the builder tried.
- **A soft critic.** Say "harsh" in the prompt and give it a binary job: which one is better, A or B. Scores out of 10 drift upward every round.
- **Named exit after N rounds.** The exit is winning the comparison, or the user stopping the run. Never a round count.
- **Over-specifying.** Every extra instruction is one fewer decision the agent makes with its own judgment. Minimal wins.

---

Ported from https://github.com/robonuggets/gauntlet-loop (CC BY 4.0). Technique originally by Matt Shumer, packaged as a skill by robonuggets.
