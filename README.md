# The Office

The Office is a local-first platform for coordinating software work across one or more Git repositories with Claude Code and Codex.

It gives every onboarded project a persistent Manager & Tech Lead, a reusable team of coding agents, repository-aware context, and a controlled path from task intake to reviewed code. Work stays on your computer: the service binds to `127.0.0.1`, uses your locally authenticated agent CLIs, and only operates on repositories you choose.

![The Office coordinating work on a repository floor](assets/the-office.png)

## What the platform does

- Onboards an existing Git repository, clones a remote repository, groups several repositories into one project floor, or creates a brand-new app from a refined text brief.
- Builds durable context about architecture, conventions, tests, risks, and important files before accepting implementation work.
- Routes a task to the correct project or projects through reception, then lets each project lead plan the work.
- Pauses ambiguous tasks with persistent **Needs your input** cards, shares answers across related floors, and resumes planning when you click **Continue**.
- Uses an optional Office-wide repository context folder to help reception identify affected repositories and propose missing floors for clone/onboarding.
- Accepts direct floor tasks with optional discussion; **Send it** carries the original notes, completed manager responses, and user follow-ups into task specifications and planning.
- Reuses persistent Claude Code or Codex sessions so project knowledge carries across questions, reports, and tasks.
- Delegates independent workstreams to a stable five-agent team while protecting overlapping repository paths.
- Captures live activity, logs, changed files, test results, token usage, and task history.
- Creates a recoverable Git checkpoint before implementation and retains an immutable completion snapshot.
- Presents the real working-tree diff for review, including selectable hunks, before anything is published.
- Recovers pending reviews after reloads and surfaces both approved results and reviews with blockers.
- Optionally monitors published PR checks, reports failures, and proposes employee fixes for your approval.
- Runs a floor's app in an embedded live preview, streams its console, and provides a small OpenAPI-aware backend request tester.
- Converts every change requested from live preview into a floor employee task with ordinary checkpoints and review; preview never edits through a privileged direct-agent path.
- Keeps rejected edits local and allows a completed run to be restored to its pre-run checkpoint.
- Pushes a task branch and opens GitHub pull requests after confirmation, or under an explicitly configured clean-review auto-publish policy.
- Stores floors, project context, conversations, runs, logs, and settings in a local SQLite database.

The Office also supports read-only codebase questions, implementation reports, Markdown specification trackers, repository file and Git tools, scoped command execution, lifecycle hooks, per-floor permissions, MCP servers, and Claude plugins.

## How work moves through The Office

```text
Task enters reception
        |
        v
Repository ownership is identified
        |
        v
Each Manager & Tech Lead plans its project work
        |
        +-- Missing decision → Needs your input → Continue planning
        |
        v
One or more agents implement and verify changes
        |
        v
The combined diff and tests are reviewed
        |
        v
The user chooses whether to publish
        |
        v
One branch and pull request is created per changed repository
```

The lead coordinates and reviews rather than implementing directly. Cross-project work is split into repository-owned assignments, and floor leads can request targeted read-only context from each other before finalizing a plan.

## Requirements

- Python 3.10 or newer
- Git
- A modern browser
- At least one locally authenticated agent CLI:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  - [Codex CLI](https://github.com/openai/codex)
- Authenticated GitHub CLI (`gh`), when creating pull requests or monitoring their checks

The Python service uses only the standard library; no Python package installation is needed.

## Quick start

Clone or copy the repository, then run:

```sh
cd /path/to/office
python3 office_server.py
```

Open <http://127.0.0.1:8765>, select **+ floor**, and provide a local repository path or Git URL.

You can use a different port:

```sh
python3 office_server.py --port 9000
```

Do not open `office.html` as a `file://` page. Repository access, agent execution, persistence, Git operations, and publishing require the local Python service.

For a guided first run, see [Getting started](docs/getting-started.md).

## Assigning tasks and sharing repository context

For an onboarded floor, enter a task title and click **Send it** to queue work for its manager. **Discuss** is optional. If you discuss first, the completed conversation is included in the task context when you send it, along with any additional text still in the message field. A failed discussion does not prevent direct submission; agent execution errors still need to be resolved before implementation can proceed.

If planning needs a user decision, a **Needs your input** card appears below reception. Choose a suggested answer or type your own, then click **Continue**. Questions, draft answers, and submitted decisions are saved across reloads. Clear tasks proceed automatically; minor assumptions are included in the plan. For reception tasks, related floors finish intake before implementation starts, and submitted answers are shared across those routes. Unrelated queued tasks can continue while a task waits for input.

For cross-repository work, use reception. You can configure a shared knowledge folder under **More → Preferences → Repository context folder → Browse folders → Save**. This global setting persists across Office restarts. Clear the path and save to disable it.

The folder can contain your own Markdown, text, RST, YAML, or JSON documents describing repository locations, owners, dependencies, and conventions. Reception uses bounded, task-relevant excerpts to inform routing. Missing repositories are proposed through the existing floor setup screen, where you choose local paths or clone destinations. The original task waits for onboarding and is then routed again. If the context is absent, unavailable, or irrelevant, reception uses normal floor allocation. No organization-specific knowledge is bundled; selected excerpts are sent to your configured agent.

All Codex workflows default to `gpt-6-astra`, including resumed sessions. Use a Codex CLI version that supports Astra; an older executable can reject the model even when discussion or task setup works. `TASK_OFFICE_CODEX_MODEL` overrides the model Office-wide. Claude-configured floors continue to use Claude. See [Agent configuration](docs/configuration.md#agent-configuration).

If a submitted task fails during planning, inspect its profile log for the provider error. The server now surfaces structured agent errors instead of only an exit code. After updating the backend, restart Office and retry the existing failed task; its saved discussion context is retained.

## Onboarding and live activity

Click the Manager & Tech Lead card to see live activity, including while it is learning a codebase. Switching a floor between Claude and Codex clears provider-specific lead and employee sessions and starts fresh onboarding; provider changes are blocked while the lead is active.

Imported conversation history shares a 60,000-character onboarding budget across selected sessions. Large sessions contribute recent excerpts, and Office reports when history is shortened. This prevents oversized history imports from exhausting the run request budget; it is not a full-history summary. See [History configuration](docs/configuration.md#local-history-import).

## Reviewing, publishing, and monitoring PRs

Completed reviews survive browser reloads: Office reconnects to the saved run instead of rerunning implementation. Both approved reviews and results with blockers open the review dialog. Inspect the real diff and select the files or hunks you want to publish; rejected changes remain local.

For a blocked review, **Push anyway & create PR** offers an explicit override. You must confirm the unresolved findings, which are included in the PR description. Automatic publishing still requires a clean review; an override does not mark the underlying review as approved.

Enable **Monitor published PR checks** in the review dialog before or after publishing. Reopen **PR created · checks**, or use the floor's **PR checks** button to inspect older PRs even while another task is running.

- Monitoring is optional, persists across reloads, and polls about once a minute per PR while the Office page is open. It stops for closed or merged PRs and resumes enabled monitoring when you reopen Office.
- Failures produce notifications and show failed checks, available GitHub Actions log excerpts, and suggested next steps. Suggestions are evidence-based investigation guidance, not a verified root-cause diagnosis. Missing checks remain waiting; connectivity and authentication errors are not reported as passing tests.
- **Assign fix to employees…** asks for confirmation before queuing implementation through the normal floor employee workflow. The task includes the PR, commit, failure evidence, and suggested investigation. Repeated polls do not duplicate alerts or fix tasks for the same failure.
- CI fixes require human review before publishing, even on a floor that otherwise auto-publishes clean reviews. Monitoring itself never modifies code, pushes, or merges.
- After CI fixes are reviewed, **Push fix to existing PR** commits selected changes to that PR's existing source branch and pushes without creating another branch or PR. Office verifies the source repository, current branch, remote PR head, and reviewed patch before publishing. A mismatch stops the operation; Office never force-pushes or merges. Monitoring refreshes after a successful push if enabled. A failed push preserves the local commit and reports it for manual recovery.

Monitoring uses your authenticated `gh` through read-only [PR inspection](https://cli.github.com/manual/gh_pr_view) and [failed-job log inspection](https://cli.github.com/manual/gh_run_view). Restart the Python service and refresh the browser after updating Office to load backend and UI changes together.

## Documentation

The repository includes task-oriented documentation for users and operators:

- [Documentation home](docs/README.md) — choose the right guide.
- [Getting started](docs/getting-started.md) — install, start, and onboard your first project.
- [Platform concepts and workflows](docs/platform-guide.md) — floors, leads, agents, routing, reports, specs, review, and publishing.
- [Configuration reference](docs/configuration.md) — CLI discovery, data storage, history, models, hooks, permissions, MCP, and plugins.
- [Operations and security](docs/operations.md) — persistence, backups, recovery, troubleshooting, and the deployment boundary.

## Architecture

The platform is intentionally build-free and runs as a personal local service:

```text
Browser client
    |
    v
Python HTTP service ---- SQLite state and run history
    |          |
    |          +---- Git repositories and checkpoints
    |
    +---- Claude Code / Codex CLI processes
                     |
                     +---- optional MCP servers and Claude plugins
```

Key source areas:

```text
office_server.py   Local HTTP service and agent-run orchestration
office_agents.py   Claude Code and Codex adapters
office_pr_checks.py Read-only GitHub check inspection and suggested next steps
office_backend/    Persistence, Git, repository, and HTTP modules
office.html        Browser application shell and shared workflow state
ui/                Build-free UI modules
the-office-plugins/ Bundled Claude plugin marketplace
test_office_server.py
                   Persistence, Git, agent, and HTTP behavior tests
docs/              User and operator documentation
```

## Development

Run the test suite with:

```sh
python3 -m unittest -v
```

With Node.js available, also verify task context, clarification and cross-floor intake, review recovery, publishing overrides, and PR-monitoring flows:

```sh
node test_task_context.cjs
node test_task_clarification.cjs
node test_review_recovery.cjs
node test_review_override.cjs
node test_pr_monitor.cjs
node test_worker_review_handoff.cjs
node test_pr_fix_publish.cjs
```

The service is implemented with the Python standard library and plain browser modules, so no package installation or asset build step is required.

## Security boundary

The Office is a personal local application, not a multi-user web service. It has no application-level authentication, and agent processes inherit the permissions of the user running the server. Do not expose it directly to a LAN or the public internet.

See [Operations and security](docs/operations.md) before using it with sensitive repositories or extending its deployment model.
