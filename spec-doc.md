# the-office — Local Agent History Import (Phase 11)

Continues the numbering from the main spec (`office-usability-spec.md`) and
`office-ai-onboarding.md`'s Phase 10 — this is Phase 11, but stands alone as
its own doc since it's a distinct, self-contained piece of work.

**Context:** Before a repo is ever onboarded into the office, the user may
already have real history with it via the bare `claude` or `codex` CLIs —
past architecture discussions, past debugging sessions, decisions that
never made it into a commit message. Both CLIs already write this to disk
locally. Right now onboarding ignores it entirely and starts from zero.
This phase makes onboarding (and refresh) aware of it.

**Confirmed storage locations** (verified against current docs/community
reporting, not assumed):
- **Claude Code**: `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`,
  one file per session, keyed by the working directory the session ran in.
  There is a defined path-encoding scheme, but the exact algorithm should be
  verified empirically (`ls ~/.claude/projects/` against a known repo path)
  before being hardcoded — don't guess it.
- **Codex**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` transcripts, and
  separately `~/.codex/state_5.sqlite` with a `threads` table containing
  `cwd`, `title`, `rollout_path`, `source`, and `model` columns — queryable
  directly by working directory, no directory scanning required.

Both are genuinely different JSONL entry formats (Claude Code:
`user`/`assistant`/`summary`/`file-history-snapshot`; Codex: compacted
records, `function_call_output`, `reasoning`, `turn_context`), so this is
CLI-specific work that belongs behind the adapter seam
(`office_backend/agents.py`, `adapter_for()`), not a one-off scanner
bolted elsewhere.

---

## 11.1 Detect existing local history for a repo path

**Problem:** There's currently no check for whether prior local CLI
history exists for a repo before onboarding builds context from scratch.

**Solution:**
- Add a method to each CLI's adapter: `local_history_for(repo_path) ->
  list[SessionRef]`, where `SessionRef` is at minimum `{session_id, path,
  modified_at, size_bytes}`.
- **Claude Code adapter:** resolve `repo_path` to its encoded project
  directory under `~/.claude/projects/`, list `.jsonl` files there.
- **Codex adapter:** `SELECT session_id, rollout_path, title, model FROM
  threads WHERE cwd = ?` against `~/.codex/state_5.sqlite` (read-only
  connection — never write to a CLI's own state).
- This is a detection step only — it returns what exists, it does not read
  or summarize content yet.

**Acceptance criteria:**
- Given a repo path with real prior Claude Code and/or Codex usage,
  `local_history_for()` returns an accurate, non-empty list for each CLI
  that has history there.
- A repo path with no prior history returns an empty list, not an error.

## 11.2 Opt-in surfacing, not silent ingestion

**Problem:** Reading a user's entire local conversation history
automatically, without asking, is the kind of thing that should require
explicit consent even in a single-user local tool — some of that history
may be unrelated to the codebase, stale, or just not something the user
wants folded into a new floor's permanent context.

**Solution:**
- During onboarding (and during 3.4's refresh), after 11.1 detects history,
  surface it as a plain count and date range: "Found 14 prior Claude Code
  sessions (Jan–Aug) and 3 Codex sessions (May–Jun) for this path. Include
  as context?"
- Default to **not** including it. Including it is an explicit action per
  floor, not a checkbox pre-checked on by default.
- This applies per CLI independently — a floor using Claude Code shouldn't
  be blocked on Codex history existing or vice versa.

**Acceptance criteria:**
- No local history is read into a floor's context without an explicit,
  visible confirmation naming what was found.
- Declining leaves onboarding exactly as it behaves today — this is purely
  additive.

## 11.3 Bounded summarization, not raw replay

**Problem:** Raw transcripts are not a reasonable thing to feed into
onboarding context directly — Codex rollout files have been observed in
the hundreds of MB to multiple GB range in real usage (compaction history
and tool output accumulate over a long session). Feeding that directly
into a prompt is both impractical and would blow past any reasonable
context budget.

**Solution:**
- Cap what's considered: most recent N sessions (configurable, sensible
  default e.g. 10) and a per-session byte cap when reading a transcript
  file, reading only the tail if a file exceeds it.
- Extract only the conversational content — Claude Code's `user`/
  `assistant` entries, Codex's actual message/reasoning entries — skipping
  raw tool output blobs, `function_call_output` payloads, and compaction
  records, which are the actual source of the multi-GB file sizes seen in
  the wild.
- Run the extracted, bounded content through a summarization pass shaped
  like `ONBOARD_SCHEMA` (architecture, conventions, risk areas, key files)
  so it merges cleanly into existing onboarding context rather than
  becoming a separate, differently-shaped blob.
- This summarization call is itself a good candidate for
  `LIGHTWEIGHT_RUN_TYPES` treatment if the volume of input is small enough,
  though a large history import may genuinely need the full model —
  don't force it onto the fast classifier if quality would suffer.

**Acceptance criteria:**
- A single 700MB+ rollout file does not get read in full; only a bounded
  tail/sample is processed.
- The resulting summary is shaped like the rest of onboarding's context,
  not a separate raw-history section bolted on.

## 11.4 Delta import on refresh, not full rescan

**Problem:** Once a floor has already imported local history once, doing
the full 11.1–11.3 pipeline again on every 3.4 refresh reprocesses
sessions that haven't changed.

**Solution:**
- Record which session IDs (per CLI) have already been imported for a
  floor.
- On refresh, only run 11.1's detection for sessions newer than the last
  import, and only summarize (11.3) the delta.

**Acceptance criteria:**
- Refreshing a floor with no new local sessions since the last import is a
  no-op for this feature specifically (though other refresh work per 3.4
  still runs).
- Refreshing after new local CLI usage picks up only the new sessions, not
  a full reprocess.

## 11.5 Privacy framing

**Problem:** This feature reads conversation history the user may not have
thought of as "office" data — worth being explicit about, even for a
local-only, single-user tool.

**Solution:**
- State plainly, at the point of the 11.2 confirmation, what's being read
  and from where (e.g. "from `~/.claude/projects/...`"), not just "include
  history? yes/no."
- This data joins the same local SQLite store the rest of the office's
  context lives in (per the existing Persistence section) — no new
  storage location, no network transmission, consistent with the office's
  existing local-only security model.

**Acceptance criteria:**
- The confirmation step names the actual file path(s)/data source being
  read, not just an abstract "history" label.

---

## Phase 12 — Spec doc intake & phased tracking

Unrelated to Phase 11 above (local history import) — a separate capability
appended to this doc at the user's request rather than getting its own
file. This is about the office being able to take in a spec document like
this one, and — when it has phases that won't all be implemented in one
sitting — track and persist that progress durably, in the repo itself,
rather than only in the office's own SQLite state.

### 12.1 A spec intake surface
**Problem:** Right now the only way to hand the office a large planning
document is to paste its contents into a single reception task, which
loses any structure the document had (phases, sub-items, acceptance
criteria) and tempts the "implement everything at once" failure mode
already flagged elsewhere as a bad idea.

**Solution:** Add a dedicated intake surface (separate from the ordinary
reception task box) where a user pastes or uploads a full spec document.
Target a floor, same as reception does today.

**Acceptance criteria:**
- A pasted markdown spec document is accepted as its own object, not
  flattened into a single task's text field.

### 12.2 Parse phase/item structure from the document
**Problem:** A spec like this one has real structure — `## Phase N` headers,
`### N.M` sub-items, each with problem/solution/acceptance-criteria — that's
lost if the document is treated as an opaque blob.

**Solution:** Parse the pasted markdown for its heading structure into:
```
{
  "title": str,
  "phases": [{
    "id": str,           # e.g. "Phase 3" or a slug
    "title": str,
    "items": [{"id": str, "title": str, "body": str, "status": "pending"}]
  }]
}
```
Parsing should be a plain markdown-structure parse (headers and their
content), not a model call — this is a mechanical parsing task, not a
judgment call, matching the same "don't ask the model to do what code can
do deterministically" principle used in Phase 10's onboarding design.

**Acceptance criteria:**
- A spec doc using the `## Phase N` / `### N.M` convention (as all of this
  doc-set does) parses into a structured, itemized tracker without manual
  reformatting.

### 12.3 Persist the spec into the repo itself
**Problem:** If tracking only lives in the office's own SQLite state,
it's invisible to anyone looking at the actual repo, disappears if the
office's database is ever reset, and isn't reviewable via ordinary git
history the way the rest of the codebase is.

**Solution:**
- Write the parsed spec to a real file in the target repository — default
  to a `docs/specs/<slug>.md` path (reusing an existing `docs/` convention
  if the repo has one; otherwise create it) — and let this be overridden
  per floor.
- The stored file should be the parsed structure rendered back to
  markdown, checklist-style (`- [ ]` / `- [x]` per item), so it's a normal,
  human-readable file — not a JSON blob — that happens to also be
  machine-parseable.
- This is a real file in the working tree, so it's subject to the same
  commit/publish flow as any other change — it doesn't bypass review to
  land in the repo.

**Acceptance criteria:**
- After intake, a real markdown file exists in the repo at the configured
  path, checklist-formatted, reflecting the spec's current phase/item
  status.
- No spec content is stored *only* in SQLite — the repo file is the
  durable source of truth.

### 12.4 Assign one phase at a time, not the whole document
**Problem:** The whole reason this feature is being asked for is the
already-identified failure mode: handing an agent a sprawling multi-phase
spec as one task produces a tangled diff with no natural checkpoint.

**Solution:**
- From the stored spec's phase list, let the user pick one phase and send
  *only that phase's items* to the floor as a task (via reception or the
  floor's own assign action) — the task text should be built from that
  phase's parsed content, not the whole document.
- Phases don't have to be done in document order — expose them as
  independently selectable, since (as this doc-set's own implementation
  order sections show) real dependencies between phases don't always
  match their numbering.

**Acceptance criteria:**
- Assigning "Phase 3" to a floor produces a task scoped to Phase 3's
  items only; the rest of the document's phases remain untouched and
  still marked pending.

### 12.5 Update the stored checklist as work completes
**Problem:** Without a feedback loop, the repo's spec file goes stale the
moment work starts — it'll say "pending" forever regardless of what
actually got built.

**Solution:** When a task that was assigned from a specific phase (12.4)
completes and is published, update that phase's items to checked/done in
the stored spec file (12.3) as part of the same commit, or a follow-up
commit — either way, the checklist should reflect reality without a
separate manual edit.

**Acceptance criteria:**
- Completing and publishing work assigned from "Phase 3" flips Phase 3's
  items to checked in the repo's spec file, visible in that commit's diff
  like any other change.

### 12.6 Multi-repo specs (note, not a full spec)
Some spec documents (like the one that produced this repo's own Phase
0–9) legitimately span multiple repos/cupboards, not just one floor. Full
support for that is a larger design question than this phase covers —
worth flagging as a known gap rather than pretending 12.1–12.5 already
handle it. A reasonable starting point: let intake target a specific
floor's primary repo for storage (12.3), even when a phase's items
reference work in other floors, and rely on the existing multi-floor
routing (reception, `floor_calls`) to actually carry out cross-repo items —
but the stored checklist itself would still only live in one place.