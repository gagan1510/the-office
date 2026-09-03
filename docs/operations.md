# Operations and security

## Deployment boundary

The Office is designed for one user on one computer. The HTTP server binds to `127.0.0.1` and has no application-level authentication.

Agent and Git processes inherit the filesystem permissions and environment of the user who starts the server. Depending on floor configuration and user approval, an implementation run can edit onboarded repositories, execute commands, use the network, or call external tools. Publishing can stage approved changes, create commits, push branches, and open pull requests.

Do not expose the service directly to a LAN or the public internet. A shared deployment would require authentication, authorization, per-user process isolation, CSRF protection, encrypted server-side persistence, audit controls, and strict repository boundaries that this project does not provide.

## Persistence and backups

The authoritative state is stored in SQLite at:

```text
~/.local/share/the-office/office.db
```

It includes floor and repository configuration, project context, selected agents, worker and task state, conversations, session IDs, retained runs, and logs.

State writes are atomic and revision-checked. A stale browser tab cannot silently overwrite a newer revision; reload that tab if the application reports a storage conflict. Browser storage is retained as an emergency fallback, but SQLite remains authoritative after migration.

The service keeps a pre-write state snapshot at most once every five minutes, retaining the latest 20. Startup also creates a full SQLite backup and retains the latest 10 under:

```text
~/.local/share/the-office/backups/
```

When `TASK_OFFICE_DATA_DIR` is set, the database and `backups/` directory are placed there instead.

Export the current logical state as JSON while the service is running:

```sh
curl -s http://127.0.0.1:8765/api/state/export > office-state.json
```

Treat database files, backups, and exports as sensitive. They may contain repository paths, prompts, conversations, logs, and agent session identifiers.

## Run recovery

The service creates a repository checkpoint before every implementation run. From the activity view, **Revert this run** restores content to that point. Because restoration can overwrite edits made after the run, inspect the current working tree first and confirm the action carefully.

On restart, unfinished runs are marked interrupted. Their prompts, logs, checkpoint references, and other retained metadata remain available, and the run can be retried. The completion snapshot for a finished run provides an immutable record of its resulting diff.

## Updating repository context

Use pull and refresh on a clean repository to fast-forward from its upstream and rebuild stale onboarding context. For cupboard floors, individual repositories can be refreshed from the dependency view.

The refresh operation deliberately stops when the working tree is dirty or when no suitable upstream exists. Resolve or preserve local work and configure repository tracking before trying again.

## Troubleshooting

### The local agent service is offline

Run `python3 office_server.py` from the project directory and open the printed HTTP address. Opening `office.html` directly does not start repository, persistence, or agent services.

### Claude or Codex is not found

Confirm the CLI runs in the same terminal environment. Set `TASK_OFFICE_CLAUDE_BIN` or `TASK_OFFICE_CODEX_BIN` to an absolute executable path when it is installed outside the detected locations.

### Claude reports an invalid API key

An inherited `ANTHROPIC_API_KEY` may override the local Claude login. The service retries a rejected key once without it. You can also remove the stale value before startup:

```sh
unset ANTHROPIC_API_KEY
python3 office_server.py
```

### Codex reports an untrusted or non-Git directory

Restart the current version of the service. Chat Room runs include the non-Git-directory override; floor runs still operate inside their configured Git repository.

### A manually pushed branch still shows publishing controls

Repository checks run every 30 seconds. Wait for the next check and refresh the page. Confirm that the working tree is clean and the current commit exists on the corresponding remote branch.

### Pull-request creation fails

Check authentication and the remote:

```sh
gh auth status
git remote -v
```

The repository needs an `origin` remote the current user can push to, and `gh` must be authenticated for that host.

### A run shows only a short failure message

Open the Manager & Tech Lead or worker activity view for the complete retained CLI log. Human-readable activity is shown by default, with raw JSON available for debugging.

### The application reports a storage conflict

Another browser tab saved a newer state revision. Reload the stale tab instead of forcing another write.

### The SQLite database cannot be opened

Check that the startup `Storage:` path exists and is writable by the current user. If `TASK_OFFICE_DATA_DIR` is set, it must be an absolute path to a writable local directory.

## Verification

Run the repository test suite after changing the service or persistence behavior:

```sh
python3 -m unittest -v test_office_server.py
```
