# Platform concepts and workflows

## Floors, leads, and workers

A **floor** represents one software project. It can own a single Git repository or a cupboard containing multiple repositories. Floor state persists across server restarts.

Each floor has one **Manager & Tech Lead** and five named **workers**:

- The lead maintains the project session, analyzes incoming work, coordinates workers, and reviews their combined output.
- Workers implement distinct workstreams or produce read-only reports. They return to the available pool when their assignment finishes.
- Multiple workers share the project workspace. The plan declares repository-relative ownership, and overlapping paths are serialized to prevent concurrent edits to the same area.

The selected Claude Code or Codex CLI is configured per floor. Changing MCP or plugin configuration clears stored floor session IDs so a later run cannot silently resume with an obsolete tool set.

## Reception and routing

Reception is the platform-level task entry point. It compares a request with the stored summaries for every floor and creates one or more repository-scoped assignments.

Use reception for cross-project work or when ownership is unclear. Assign directly from a floor when ownership is already known.

A busy lead keeps new assignments in a durable inbox and processes them in order. Before finalizing a plan, a lead can make one round of targeted, read-only consultation calls to other floors. Other floors provide context only unless reception explicitly routes implementation work to them.

## Implementation lifecycle

Before an implementation run begins, The Office creates a Git checkpoint under `refs/the-office/checkpoints/run-<id>` for every affected repository. If the floor requires permission approval, the process waits until the user approves its file, shell, network, or external-tool capabilities.

The lead then chooses a single-worker or multi-worker plan. Independent paths may run concurrently; overlapping ownership waits on a repository-scoped lock. Locks are released on success, failure, or cancellation and retained appropriately for retry recovery.

During a run, the activity view shows structured CLI activity, logs, changed files, test phases, and usage estimates. The service retains run metadata and bounded logs in SQLite. A server restart marks unfinished work as interrupted without discarding its retained prompt, logs, or checkpoint.

## Questions and reports

Use **Ask** on a floor for a read-only answer grounded in its onboarding context and repository. A question may name another onboarded floor when cross-project context is needed. Questions cannot run while that lead is handling another turn.

Use **Reports** for deeper read-only investigation into a feature, migration, integration, or architectural change. One available worker researches the repository independently and returns:

- an executive summary;
- a recommended approach;
- ordered implementation steps;
- risks and tradeoffs; and
- relevant repository files.

Reports remain in floor history and their summaries can inform later work. The separate **Chat Room** is for ordinary Claude or Codex conversation and does not receive permission to modify repositories or take external actions.

## Specification trackers

Open **Specs** to paste or upload a Markdown specification and choose its owning floor. The document should use `## Phase` and `### item` headings.

The platform parses that structure without a model call and writes a checklist under `docs/specs/` in the selected repository, unless you provide another safe relative path. That file becomes the durable source of truth and remains an ordinary working-tree change.

Phases are assigned independently. Only the selected phase enters the floor inbox. When reviewed work is published, its checklist items are checked and staged in the same commit. A multi-repository effort can keep its tracker in one primary repository while reception routes implementation assignments to the other owners.

## Review and recovery

When implementation finishes, The Office presents the actual staged, unstaged, and untracked changes. You can inspect the diff, edit the modified side, accept or reject individual hunks, and refresh the review digest.

Rejected hunks remain in the working tree. Immediately before publication, the server regenerates the diff and verifies that the repository has not changed since review. It stages only the selected patch and stops if the reviewed state is stale or a changed cupboard was omitted.

Every retained run has two useful recovery points:

- the pre-run checkpoint, which can restore tracked and untracked content; and
- an immutable completion snapshot, which preserves the exact diff produced by that run.

Restoring a checkpoint can overwrite later edits in the same repository. The action is disabled while that repository has an active run and always requires confirmation.

Failed or server-interrupted runs can be retried from their retained prompt and checkpoint.

## Publishing

Nothing is pushed automatically. After review, publication asks for a task-specific source branch and a destination branch, then requests explicit confirmation.

For each changed repository, The Office:

1. creates the source branch;
2. stages the approved patch;
3. creates a commit;
4. pushes the branch; and
5. runs `gh pr create --base <destination-branch>`.

The GitHub CLI must be authenticated, and the repository must have an `origin` remote the current user can push to. The platform also detects manually pushed work and hides stale publishing controls after it confirms a clean commit on a remote branch.

## Repository workspace

The floor workspace can browse and edit repository files, render linked Markdown documentation, inspect branches and commits, stash or restore work, and discard one explicitly confirmed file change. It can also run a plain shell command or detected package, Make, Python, Rust, or Go commands inside the selected repository while streaming output.

These tools operate with the permissions of the local user running The Office. Configure a permission approval gate when a project requires an explicit capability check before implementation.
