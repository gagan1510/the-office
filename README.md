# the office

the office is a local-first browser interface for coordinating coding work through the Claude Code and Codex CLIs. Each repository is represented as a floor with a user-named Manager & Tech Lead, a persistent agent session, and a permanent team of five employees.

The application runs entirely on your computer. The Python server listens only on `127.0.0.1`, launches your locally installed agent CLIs, and gives them access to repositories you explicitly onboard.

![the office showing a repository floor with its manager, codebase chat, and five permanent employees](assets/the-office.png)

## Features

- Choose Claude or Codex independently for each floor.
- Onboard an existing local Git repository with the built-in folder browser.
- Clone a Git repository into a selected local directory.
- Build repository context before accepting implementation work.
- Keep one persistent Manager & Tech Lead session per floor.
- Give every floor a stable team of five named employees who are reused across tasks.
- Let the lead analyze each task and decide whether it needs one worker or multiple workers.
- Queue reception and floor assignments durably while the shared lead session is busy.
- Delegate multi-worker work through the CLI's native subagent support.
- Return workers to the available pool as soon as implementation finishes.
- View human-readable, live CLI activity by clicking a profile or worker.
- See live employee phases such as reading code, editing files, running tests, building, reviewing, and coordinating delegated work.
- Ask the floor lead questions about the onboarded codebase.
- Use the separate Chat Room for ordinary Claude or Codex conversations.
- Review combined changes before publishing.
- Push a branch and create a GitHub pull request only after explicit confirmation.
- Detect manually pushed work across all floors and hide stale publish controls.
- Store floors, chat history, session IDs, and office state in browser `localStorage`.

## Requirements

- Python 3.10 or newer
- Git
- At least one supported local agent CLI:
  - Claude Code, authenticated with the user's local Claude account or a valid API key
  - Codex CLI, authenticated locally
- A modern browser
- GitHub CLI (`gh`) authenticated with GitHub, only if you want the office to create pull requests

There are no third-party Python packages. `requirements.txt` is intentionally empty apart from explanatory comments.

Verify the tools you intend to use:

```sh
python3 --version
git --version
claude --version
codex --version
gh --version
```

the office also searches common user installation locations such as `~/.local/bin`, npm global bins, Volta, Bun, and installed NVM versions.

## Installation

Clone or copy this directory, then optionally create a virtual environment:

```sh
cd /path/to/office
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

No pip packages will be installed; the virtual environment simply gives the server an isolated Python runtime.

## Running

Start the local service:

```sh
python3 office_server.py
```

Open:

```text
http://127.0.0.1:8765
```

Use another port if necessary:

```sh
python3 office_server.py --port 9000
```

Do not open `office.html` directly as a `file://` page. Agent execution, logs, repository browsing, Git checks, cloning, and publishing require the Python service.

## CLI configuration

If an agent executable is not discoverable automatically, provide its absolute path before starting the server:

```sh
export TASK_OFFICE_CODEX_BIN=/absolute/path/to/codex
export TASK_OFFICE_CLAUDE_BIN=/absolute/path/to/claude
python3 office_server.py
```

The server prints the detected Claude and Codex paths at startup.

Claude normally uses the authentication available to the local CLI. If an inherited `ANTHROPIC_API_KEY` is rejected, the office retries once using the user's local Claude login. Codex Chat Room sessions are allowed to run from the non-Git application directory and remain read-only.

## Creating a floor

1. Select **+ floor**.
2. Enter a repository/floor name.
3. Choose Claude or Codex.
4. Enter the Manager & Tech Lead's display name. The supplied name is used throughout the interface and activity history.
5. Choose one repository setup:
   - **Existing repository:** browse to a local folder containing `.git`.
   - **Clone repository:** enter a Git URL and choose the parent destination folder.
6. Save the floor.

The selected agent first inspects the repository in a read-only onboarding run. It records a structured summary of the architecture, conventions, test commands, risk areas, and important files. Tasks remain unavailable until onboarding succeeds.

## Work lifecycle

```text
User task
   ↓
Persistent floor inbox
   ↓
Manager & Tech Lead analyzes the task
   ↓
Single-worker or multi-worker decision
   ↓
Worker delegation inside the shared floor session
   ↓
Worker desks return to the available pool
   ↓
Combined diff and test review
   ↓
User chooses whether to publish
   ↓
Branch push and GitHub pull request
```

The Manager & Tech Lead coordinates and reviews; it does not take implementation work directly. For multi-worker tasks, it assigns distinct workstreams, waits for the workers, and reviews their combined result.

Pam and the floor assignment form both submit to the same persistent floor inbox. If the lead session is already planning, orchestrating, reviewing, answering a question, or recovering after a refresh, new work stays visibly queued and is dispatched in order when the session becomes available. Worker desks are released after implementation completes; user approval, merging, and publishing do not keep them occupied.

Nothing is pushed automatically. The publish action shows the proposed branch, commit/PR title, and PR body, then asks for confirmation before it changes branches, stages files, commits, pushes, or invokes `gh pr create`.

## Repository push detection

For every floor with an approved review, the office checks whether the current work has already been pushed. It considers:

- working-tree changes;
- commits ahead of the configured upstream;
- local remote-tracking branches containing `HEAD`; and
- the matching branch on `origin` when local tracking metadata is missing.

Checks refresh every 30 seconds. Once a clean current commit is confirmed on a remote branch, the stale review/publish control is hidden. This also covers branches pushed manually without `git push -u`.

## Chat and codebase questions

The **Chat Room** is separate from the office floors. Choose Claude or Codex and use it as a plain conversational assistant. Chat Room prompts prohibit repository edits and external actions.

Each floor also has an **Ask _name_** section. Those answers come from the persistent floor session and include the repository context gathered during onboarding. Codebase questions are read-only and cannot be asked while the lead is handling another turn.

## Logs

Click the Manager & Tech Lead or a worker profile to see the actual local CLI activity for its shared session. Logs are rendered in a concise human-readable format by default; raw JSON remains available from the log toolbar for debugging.

Employee desks also show the latest activity inferred from structured Claude/Codex events. File-write tools appear as **Editing code**, common test commands appear as **Running tests**, and read, build, review, delegation, and command activity use their own labels and colors. When a CLI exposes only the shared orchestration session rather than individual subagent identities, assigned employees display that shared session's currently observed phase.

The server holds live process output in memory, with up to 5,000 entries per profile. Restarting the server clears those in-memory logs, although browser-persisted floor and session metadata remains available.

## Persistence

Office configuration and conversation state are stored under the browser origin in `localStorage`. This includes:

- floor and repository configuration;
- selected agent and lead name;
- worker/task state;
- onboarding and review context;
- Claude/Codex session IDs; and
- Chat Room history.

Use the same browser profile and server address to retain state. Changing the port creates a different browser origin and therefore a different `localStorage` namespace. Clearing site data removes the saved office state but does not modify any repository.

## Troubleshooting

### The page says the local agent service is offline

Run `python3 office_server.py` and open the printed HTTP address. Do not use the HTML file directly.

### Claude or Codex is not found

Confirm the CLI runs in a terminal. If it is installed in a nonstandard location, set `TASK_OFFICE_CLAUDE_BIN` or `TASK_OFFICE_CODEX_BIN` to the absolute executable path before starting the server.

### Claude reports an invalid API key

An `ANTHROPIC_API_KEY` in the server environment may override the user's Claude login. The office automatically retries rejected keys with the local login. You can also remove the stale variable before startup:

```sh
unset ANTHROPIC_API_KEY
python3 office_server.py
```

### Codex says the directory is not trusted or is not a Git repository

Restart the current version of `office_server.py`. Chat Room runs include Codex's non-Git-directory override; repository floors still operate inside their configured Git repository.

### Folder selection does not open a native dialog

Use the built-in folder browser in the floor form. It does not require `zenity`, `kdialog`, or Tkinter. The server retains native-picker support as an optional fallback where one of those tools is available.

### A manually pushed branch still shows a publish button

Wait up to 30 seconds and refresh the page. Confirm that the working tree is clean and that the current commit exists on the corresponding branch on `origin`. Restart the Python server after updating its code so the repository-state endpoint is available.

### Pull-request creation fails

Verify that:

```sh
gh auth status
git remote -v
```

The repository must have an `origin` remote that the current user can push to, and `gh` must be authenticated for that GitHub host.

### The UI only shows a generic failure

New runs return a concise failure reason directly in chat. Click the relevant profile to inspect the complete local CLI log when more detail is needed.

## Security and deployment model

The office is designed as a personal local application, not a public web service:

- It binds to `127.0.0.1` and has no user authentication.
- Agent processes inherit the local user's filesystem permissions and selected CLI environment.
- Implementation agents may edit files in onboarded repositories.
- Publishing can stage all current repository changes and push them after confirmation.
- Browser state may contain repository paths, prompts, task descriptions, and session IDs.

Do not expose the server directly to a LAN or the public internet. A multi-user deployment would require authentication, authorization, per-user process isolation, encrypted server-side persistence, CSRF protection, audit controls, and strict repository access boundaries.

## Project structure

```text
office.html       Single-page interface, office state, and workflow orchestration
office_server.py  Local HTTP service, CLI execution, Git operations, and logs
requirements.txt Python dependency declaration (standard library only)
README.md         Setup and operating documentation
.gitignore        Local Python, editor, secret, log, and OS exclusions
assets/           Screenshots and other README media
```

## Stopping the server

Press `Ctrl+C` in the terminal running `office_server.py`. Active child-agent work should be allowed to finish or stopped from the relevant profile before shutting down.
