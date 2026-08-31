r"""Red tests for task 0ee4dc98 -- "The Dashboard animates the knowledge gain".

TRACE. Each test names the acceptance criterion it pins, the concrete
measurement that is RED at the base commit 1bece91b, and the file the fix
must land in. The task's `verify` pins exactly two of the functions here
(`test_a_new_sample_moves_the_chart`,
`test_a_fall_renders_as_clearly_as_a_rise`); the other three pin the
backend and guard ACs the plan lists under the same slice.

  AC-1 a finished play records ONE coverage sample, even below the floor
       RED AT BASE: `grep -n "brain_coverage_samples"
       prism_service/services/brain_health.py` -> no match at 1bece91b.
       The table and the write call do not exist, so a direct read of the
       samples table finds no such table and returns no rows.
       FIX LANDS IN: prism_service/services/brain_health.py
  AC-2 GET /api/brain/health returns `history`, oldest-first, from that
       same table
       RED AT BASE: `grep -n "history" prism_service/api/brain.py` -> no
       match. The route returns entries/indexed/ratio/measured_at only
       (api/brain.py:52-57), so `body["history"]` raises KeyError.
       FIX LANDS IN: prism_service/services/brain_health.py (the reader)
       and prism_service/api/brain.py (the field)
  AC-3 the Dashboard renders that history as a real animated chart
  AC-4 a new sample moves the chart with no page reload
       RED AT BASE: `grep -n "motion/react\|<motion\."
       prism_service/web/src/pages/DashboardPage.tsx` -> no match. The
       page imports no motion symbol at all (DashboardPage.tsx:1-9), and
       no component on it renders `<motion.circle`/`<motion.line`.
       FIX LANDS IN: prism_service/web/src/pages/DashboardPage.tsx
  AC-5 a fall renders through the SAME code path as a rise
       RED AT BASE: the chart does not exist, so the property under test
       -- one styling path regardless of direction -- is unbuilt.
       FIX LANDS IN: prism_service/web/src/pages/DashboardPage.tsx
  AC-6 no new charting dependency, no full-store reindex
       RED AT BASE: N/A -- a guard checked against the diff produced.

Every new symbol (`brain_health` sample writer and reader, the route's
`history` field, the chart component) is reached LAZILY inside a test
body, mirroring tests/unit/test_flow_keeps_the_brain_healthy.py, so a run
against the base tree is a genuine RED (rc==1, real FAILUREs) and not a
collection ERROR (rc==2), which is what the red_gate machine seat needs.

THE CHART IS FOUND BY WHAT IT RENDERS, NOT BY ITS NAME. `_chart_source`
locates the function on DashboardPage.tsx whose body actually renders a
`<motion.circle`/`<motion.line`, then returns that function's balanced
body with comments removed -- so an explanatory comment can never satisfy
an assertion, and a fixed character window can never push the real code
out of view.

The stores are DISPOSABLE sqlite/JSONL trees under tmp_path built by the
real MemoryService and BrainService -- never the live /home/siegeon/.prism
store -- so each rule is proven generically.
"""
