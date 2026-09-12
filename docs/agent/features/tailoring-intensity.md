# Tailoring Intensity

> **How strongly the tailoring pass rewrites a resume against a job description.**

The user picks an intensity on the Tailor page; the default is set in Settings
(`default_prompt_id`). The id travels as `prompt_id` on the improve/preview
request and selects a matching entry in every strategy registry.

## Options

| id | Label | Behavior |
|----|-------|----------|
| `nudge` | Light nudge | Minimal rephrasing where there is already a clear match. No new bullets. |
| `keywords` | Keyword enhance | Weaves JD keywords into existing bullets. No new bullets. **Default.** |
| `full` | Full tailor | Targeted rewrites, verified JD skills, bullets that elaborate on existing work. |
| `full_pro` | Full tailor Pro | Maximum JD coverage: rewrites bullets into JD terminology, **appends new bullets** for JD responsibilities the candidate's existing roles credibly covered, and **adds every missing required/preferred JD skill**. |

## Strategy registries

Every offered option must appear in all five registries
(`apps/backend/app/prompts/templates.py`), or the user's choice silently falls
back to the default. `tests/service/test_full_pro_strategy.py` enforces this.

| Registry | Used by |
|----------|---------|
| `IMPROVE_RESUME_PROMPTS` | Full-output fallback when the resume has no structured data |
| `CRITICAL_TRUTHFULNESS_RULES` | Anti-fabrication block injected into the full-output prompt |
| `DIFF_STRATEGY_INSTRUCTIONS` | Rule 4 of `DIFF_IMPROVE_PROMPT` (the normal diff path) |
| `DIFF_COVERAGE_INSTRUCTIONS` | `COVERAGE:` block of `DIFF_IMPROVE_PROMPT` |
| `SKILL_PLAN_COVERAGE_INSTRUCTIONS` | Rule 7 of `SKILL_TARGET_PLAN_PROMPT` (the planning pass) |

The `COVERAGE:` block exists because Pro contradicts rules 9 and 11 ("keep
changes minimal", "do not add new work"). Rather than leave the model to resolve
a silent contradiction, each strategy states explicitly how those rules apply —
non-Pro strategies say "apply rules 9 and 11 exactly as written", Pro states
that its directions take precedence where they conflict.

## What Pro does NOT loosen

Pro broadens coverage; it never loosens the fabrication guardrails. These still
bind, in the prompt and in the deterministic post-passes:

- No invented metrics (`_novel_numbers` strips any bullet that introduces a number)
- No new employers, work entries, education entries, or projects
- No new certifications, degrees, or credentials
- No changed names, companies, titles, or dates
- No removal of existing skills, languages, certifications, or awards

## Pipeline notes

- **Skill additions** are gated by `verify_skill_target_plan`: a skill is only
  addable if it is an existing resume skill, an explicit JD required/preferred
  skill, or already present in the resume text. Pro widens what the *planner*
  proposes, not what the verifier accepts.
- **Appended bullets** are permitted downstream by `allow_appended_rows`, which
  the router derives from the applied `append` changes — so this already worked
  structurally before Pro existed; Pro is what makes the model actually emit them.
- **Token budget**: Pro requests `PRO_DIFF_MAX_TOKENS` (8192) instead of 4096.
  Its change list is much longer, and a truncated JSON array loses the whole
  diff pass rather than degrading gracefully.
- Every added bullet and skill surfaces in the diff preview for explicit user
  confirmation before it is saved.

## Adding a new intensity

1. Add the option to `IMPROVE_PROMPT_OPTIONS`.
2. Add an entry to all five registries above.
3. Add `tailor.promptOptions.<id>.{label,description}` to
   `apps/frontend/messages/en.json`.
4. Add the id to the hardcoded fallback lists in `app/(default)/tailor/page.tsx`
   and the fallback + override lists in `app/(default)/settings/page.tsx`.
