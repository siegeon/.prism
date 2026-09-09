# Workflow Step → Driver Mapping

## SUMMARY
- **Workflows**: 5 (implement, triage, align_language, promote_to_law, quickfix)
- **Total steps**: 23
- **Agent steps (driven by task_runner)**: 13 ✓
- **Gate steps (driven by gate_adjudicator)**: 5 ✓ (+ NEW triage decide)
- **Special steps (intake/done)**: 5 (auto-advance, need verification)
- **GAPS**: promote_to_law review gate, quickfix verify_fix deterministic runner

## DETAILED TABLE

### implement (10 steps)
| Step | Type | Agent | Driver | File:Line | Status |
|------|------|-------|--------|-----------|--------|
| review_previous_notes | agent | sm | task_runner | task_runner.py:736 | ✓ |
| draft_story | agent | sm | task_runner | task_runner.py:736 | ✓ |
| story_gate | gate | - | gate_adjudicator | gate_adjudicator.py:194-196 | ✓ rubric story_complete |
| verify_plan | agent | sm | task_runner | task_runner.py:736 | ✓ |
| plan_gate | gate | - | gate_adjudicator | gate_adjudicator.py:194-196 | ✓ rubric plan_coverage |
| write_failing_tests | agent | qa | task_runner | task_runner.py:736 | ✓ |
| red_gate | gate | - | gate_adjudicator | gate_adjudicator.py:194-196 | ✓ demo + test proof |
| implement_tasks | agent | dev | task_runner | task_runner.py:736 | ✓ |
| verify_green_state | agent | qa | task_runner | task_runner.py:736 | ✓ |
| green_gate | gate | - | gate_adjudicator | gate_adjudicator.py:194-196 | ✓ |

### triage (4 steps)
| Step | Type | Agent | Driver | File:Line | Status |
|------|------|-------|--------|-----------|--------|
| intake | intake | - | (auto-advance?) | - | ? |
| classify | agent | sm | task_runner | task_runner.py:736 | ✓ |
| decide | gate | - | gate_adjudicator + triage_decision | gate_adjudicator.py:210-217 | ✓ NEW machine seat |
| done | done | - | (auto-advance?) | - | ? |

### align_language (4 steps)
| Step | Type | Agent | Driver | File:Line | Status |
|------|------|-------|--------|-----------|--------|
| collect | intake | - | (auto-advance?) | - | ? |
| align | agent | dev | task_runner | task_runner.py:736 | ✓ |
| verify | agent | qa | task_runner | task_runner.py:736 | ✓ |
| done | done | - | (auto-advance?) | - | ? |

### promote_to_law (4 steps)
| Step | Type | Agent | Driver | File:Line | Status |
|------|------|-------|--------|-----------|--------|
| draft | agent | dev | task_runner | task_runner.py:736 | ✓ |
| review | gate | - | ??? | - | ✗ **GAP** no driver |
| install | agent | dev | task_runner | task_runner.py:736 | ✓ |
| done | done | - | (auto-advance?) | - | ? |

### quickfix (4 steps)
| Step | Type | Agent | Driver | File:Line | Status |
|------|------|-------|--------|-----------|--------|
| intake | intake | - | (auto-advance?) | - | ? |
| apply_fix | agent | dev | task_runner | task_runner.py:736 | ✓ |
| verify_fix | agent* | - | ??? | - | ✗ **GAP** deterministic subprocess |
| done | done | - | (auto-advance?) | - | ? |

## IDENTIFIED GAPS

### High Priority (Blocks Workflows)
1. **promote_to_law::review gate** - task sits at review with no machine seat
   - validation=None (no rubric)
   - Should check draft output completeness
   - Machine seat: promote_to_law_review.py
   - Add to gate_adjudicator.py sweep list

2. **quickfix::verify_fix** - deterministic subprocess runner
   - type=agent but agent=None and validation=None
   - Runs pytest from workspace root
   - Commits/pushes if rc==0
   - May need special handling in task_runner or conductor_flow

### Medium Priority (Special Steps)
3. **intake steps** (triage, align_language, quickfix)
   - Special step type, auto-advance?
   
4. **done steps** (all workflows)
   - Terminal step type, auto-complete?

## Implementation Plan

1. Create promote_to_law_review.py machine seat (like triage_decision.py)
2. Wire into gate_adjudicator.py sweep
3. Add review-gate-check.json behavior file
4. Handle quickfix::verify_fix deterministic runner
5. Verify intake/done step auto-advancement

