# Configuration reference

The Office uses environment variables for service-wide runtime configuration and floor settings for project-specific controls.

## CLI discovery

The service searches the current `PATH` and common user install locations, including local bins, npm global bins, Volta, Bun, and installed NVM versions. If a CLI is not found, set its absolute path before startup:

```sh
export TASK_OFFICE_CODEX_BIN=/absolute/path/to/codex
export TASK_OFFICE_CLAUDE_BIN=/absolute/path/to/claude
python3 office_server.py
```

The detected paths are printed at startup.

Claude normally uses the authentication available to its local CLI. If an inherited `ANTHROPIC_API_KEY` is rejected, The Office retries once with the user's local Claude login.

## Agent configuration

Reception, planning, and review use lightweight classifier calls without changing the model used for implementation. Override their defaults with:

```sh
export TASK_OFFICE_CLAUDE_CLASSIFIER_MODEL=haiku
export TASK_OFFICE_CODEX_CLASSIFIER_MODEL=gpt-5.1-codex-mini
```

Set a variable to an empty string to let that CLI select its configured model.

Each floor can configure:

- Claude Code or Codex as its agent;
- conservative pre-run approval for file, shell, network, and external-tool capabilities;
- floor-scoped MCP server definitions;
- additional Claude plugin directories;
- manual review or opt-in clean-review auto-publish; and
- token warnings and optional input/output prices for approximate cost estimates.

MCP definitions are passed only to fresh sessions and remain floor-scoped because credentials and external-access policies may differ by project.

Every valid plugin in `the-office-plugins/plugins/` is enabled for full Claude floor runs. Valid custom Claude plugin directories saved on any floor are shared office-wide. Lightweight classification calls remain plugin-free, and Claude plugins are not passed to Codex.

## Data and retention

The default data directory is:

```text
~/.local/share/the-office/
```

Store data elsewhere with:

```sh
export TASK_OFFICE_DATA_DIR=/absolute/path/to/office-data
python3 office_server.py
```

The directory contains the authoritative SQLite database and its backups. It can include local repository paths, prompts, conversations, logs, and agent session IDs. Keep it private and do not commit it or place it in a publicly synchronized folder.

`TASK_OFFICE_RUN_HISTORY` controls retained agent-run count. The default is `200`, and the minimum is `20`. Each retained run stores at most the latest 5,000 log entries.

## Local history import

Before onboarding or context refresh, The Office can detect earlier Claude Code and Codex sessions associated with the selected repository paths. Import is always opt-in.

Control its bounds with:

```sh
export TASK_OFFICE_HISTORY_SESSION_LIMIT=10
export TASK_OFFICE_HISTORY_BYTES_PER_SESSION=262144
```

Imported session IDs are recorded with the floor so future refreshes process only newly discovered sessions.

## Lifecycle hooks

Set `TASK_OFFICE_HOOKS_DIR` to a directory containing any of these executable files:

- `on_run_finished`
- `on_run_blocked`
- `on_review_ready`

Example:

```sh
export TASK_OFFICE_HOOKS_DIR=/absolute/path/to/office-hooks
python3 office_server.py
```

The matching executable is called directly, never through a shell. It receives a bounded JSON event on standard input and has 15 seconds to finish. This is suitable for a local notification daemon or another small integration.

## Server options

Run on another loopback port:

```sh
python3 office_server.py --port 9000
```

Print the managed-settings fragment used for native Claude plugin relevance suggestions:

```sh
python3 office_server.py --print-claude-plugin-policy
```
