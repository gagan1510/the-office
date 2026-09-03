# the-office — AI-Native Floor Creation

Right now creating a floor means six form fields (name, agent, lead name,
repo mode, path/URL, cupboard toggle). Everywhere else in the office,
intent is parsed from a sentence — reception already does this for every
task via `RECEPTION_SCHEMA`. Floor creation is the odd one out. This doc
specs bringing it in line: one prompt in, a confirm step, then the same
real onboarding run that already exists.

Context for whoever implements this: this builds on `LIGHTWEIGHT_RUN_TYPES`
(the fast-classifier-model, hooks/skills-free call path already used for
`reception`/`plan`/`review`/`floor_call`), `discover_git_repositories()`
(cupboard detection), and the existing floor-creation code path
(`repo_path()`, `existing_repo_path()`, the onboarding run itself). Nothing
here replaces the real onboarding run — it only replaces the form that
currently precedes it.

---

## 10.1 A single intent-parsing endpoint for floor creation

**Problem:** The form asks the user to pre-decide things a sentence already
implies — repo mode, whether it's a cupboard folder — instead of just
accepting what they'd naturally say.

**Solution:**
- Add a new lightweight run type, e.g. `floor_intent`, to the
  `LIGHTWEIGHT_RUN_TYPES` set, so it rides the fast classifier model and
  skips hooks/skills/MCP the same way `reception` does.
- Its schema should extract only what the sentence actually states — not
  infer repo structure (that's 10.2's job, done deterministically):
  ```
  FLOOR_INTENT_SCHEMA = {
    "floors": [{
      "raw_path_or_url": str,       # exactly what the user said, unresolved
      "agent": "claude" | "codex" | null,   # null if unstated
      "lead_name": str | null,              # null if unstated
      "floor_name": str | null,             # null if unstated
    }],
    "unresolved": str | null,   # set when the prompt lacks a path entirely
  }
  ```
- One user message can name multiple floors (see 10.5) — hence a list, not
  a single object.

**Acceptance criteria:**
- A sentence like "add my smallcase-infra repo at ~/code/smallcase-infra,
  use Claude" extracts a path and an agent without the user touching a
  form field for either.
- The model is never asked to decide `local` vs `clone` vs cupboard mode —
  it only reports what the user said about the path itself (which may be a
  bare path, a URL, or missing entirely).

## 10.2 Deterministic repo-shape detection, not model guesswork

**Problem:** Whether something is an existing repo, a cupboard folder, or
needs cloning is a filesystem fact, not a judgment call — asking the model
to guess it is a place it can get a mechanically-checkable thing wrong for
free.

**Solution:** After 10.1 extracts a raw path/URL, resolve mode in plain
Python, reusing what already exists:
- Looks like a URL (`re.match(r"^\w+://")` or a common git-host pattern) →
  `clone` mode.
- Resolves to an absolute existing directory containing `.git` → `local`
  (existing-repo) mode.
- Resolves to an absolute existing directory *without* `.git` → run
  `discover_git_repositories()` against it; any hits → cupboard mode with
  those repos pre-populated; no hits → this is genuinely ambiguous, surface
  it in the confirm step (10.3) rather than guessing.
- Doesn't resolve to anything on disk and isn't URL-shaped → unresolved,
  triggers the disambiguation flow (10.4).

**Acceptance criteria:**
- Given a real absolute path, floor mode (`local`/`clone`/cupboard) is
  always determined by filesystem inspection, never by asking the
  classifier to guess.
- The one genuinely ambiguous case (a real, empty-of-git-repos folder) is
  surfaced to the user rather than silently defaulting to either mode.

## 10.3 A one-line confirm before the real onboarding run fires

**Problem:** Floor creation triggers a real (non-lightweight) onboarding
agent run — worth a sanity check before it fires, but that check shouldn't
cost six fields to get through.

**Solution:** After 10.1 + 10.2 resolve intent, show one line per floor:
> Found a git repo at `~/code/smallcase-infra` → existing-repo floor,
> Claude, lead unnamed. **Go?**

with an edit affordance for any field that's wrong (agent, lead name,
detected mode) rather than a full form. Confirming triggers the same
onboarding run the form already triggers today — this step only changes
how the input to that run gets assembled.

**Acceptance criteria:**
- No onboarding run starts without an explicit confirm.
- Correcting a wrong field (e.g. detected mode) takes one click/edit, not a
  restart of the whole flow.

## 10.4 Conversational disambiguation, not a fallback to the form

**Problem:** If the prompt is missing something essential (no path at all,
or a path that resolves to an empty non-git folder per 10.2), redirecting
to the form defeats the point of this whole feature.

**Solution:** Ask the missing thing as a follow-up message in the same
flow — "Which folder?" or "That folder has no git repos in it — is this
meant to be a cupboard root, or did you mean a different path?" — and
resume 10.1's parsing on the reply rather than starting over.

**Acceptance criteria:**
- Missing or ambiguous input never routes the user to the form as the
  recovery path; it's always a follow-up question in the same
  conversational flow.

## 10.5 Multiple floors from one prompt

**Problem:** "I've got api, web, and infra repos under ~/code, set them
all up with Claude" is a completely reasonable single request under this
model, but the current form only ever creates one floor per submission.

**Solution:** 10.1's schema already returns a list. Run 10.2's detection
and 10.3's confirm per floor (one confirm block per floor, or a single
confirm covering all of them if they're all straightforward). If Phase
0.3's parallelization exists, kick off the resulting onboarding runs
concurrently rather than one at a time.

**Acceptance criteria:**
- A single prompt naming multiple repos produces multiple confirmed floors
  without repeating the flow per repo.

## 10.6 Keep the form — as a fallback, not the default

**Problem:** Some cases are genuinely fiddly in ways a sentence doesn't
capture well — a non-default clone destination, precise cupboard
exclusions, editing a floor's config after creation.

**Solution:** Don't delete the form. Demote it: reachable from "advanced
setup" or an edit action, no longer the first thing a new user hits.

**Acceptance criteria:**
- Every field the current form exposes is still reachable somewhere, just
  not as the primary path.

---

## Framing note

This is structurally "Pam, but one layer earlier" — the same
natural-language-intent-to-structured-action pattern reception already does
for task routing, applied to floor creation instead. It's worth treating as
an extension of that existing pattern rather than a new subsystem: same
lightweight-run-type mechanism, same confirm-before-acting discipline as
the rest of the office (review before publish, approval before risky
capabilities), just applied to onboarding.