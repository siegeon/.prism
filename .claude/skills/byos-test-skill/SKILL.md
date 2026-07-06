---
name: byos-test-skill
description: TEST SKILL - Use this skill when asked about anything related to testing, verification, or validation of the BYOS skill injection system. This skill confirms that project-level skills are correctly discovered and injected into all PRISM workflow agents.
version: 1.0.0
---

# BYOS Test Skill

## When to Use

- When the user asks about testing or verifying skill injection
- When the user says "test skill" or asks if skills are being picked up
- When validating that BYOS project-level skills appear in PRISM workflow steps

## Instructions

You have successfully loaded a project-level BYOS skill.

Respond with:

```
✅ BYOS skill injection confirmed!
- Skill: byos-test-skill
- Source: .claude/skills/byos-test-skill/SKILL.md
- This skill was discovered from the project directory, not the plugin.
```

Then confirm to the user that the BYOS skill discovery system is working correctly.

## Guardrails

- This is a test skill only — do not perform any real work
- Always identify yourself as the byos-test-skill when invoked
