# Getting started

This guide takes The Office from a fresh checkout to its first repository-aware agent floor.

## Requirements

Install:

- Python 3.10 or newer;
- Git;
- a modern browser; and
- Claude Code, Codex CLI, or both, authenticated locally.

Install and authenticate GitHub CLI (`gh`) only if you want The Office to push branches and create pull requests.

Verify the tools you plan to use:

```sh
python3 --version
git --version
claude --version
codex --version
gh --version
```

Only one supported agent CLI is required. There are no third-party Python dependencies.

## Start the service

From the repository root:

```sh
python3 office_server.py
```

The startup output shows the application address, database location, and detected Claude and Codex executables. Open:

```text
http://127.0.0.1:8765
```

To choose another port:

```sh
python3 office_server.py --port 9000
```

Keep the terminal open while using The Office. Stop the service with `Ctrl+C` after active agent runs have finished or have been stopped from their activity view.

## Create a floor

A floor is the durable workspace for a project. It contains repository context, a persistent Manager & Tech Lead session, five reusable workers, settings, and history.

1. Select **+ floor**.
2. Describe what to add, such as `add ~/code/payments with Codex` or provide a Git URL.
3. Review the detected path, project name, agent, lead name, and clone destination.
4. Choose **Go** to confirm.
5. Wait for the read-only onboarding run to finish.

The Office accepts three project shapes:

- **Existing repository:** a local Git repository becomes one floor.
- **Clone:** a Git URL is cloned into the confirmed local destination.
- **Cupboard:** a local directory containing several Git repositories becomes one floor with separate context for every repository.

If a directory exists but contains no repository, confirm that it should be used as an empty cupboard root or provide a different path. No onboarding run begins before the detected setup is confirmed.

Use **Advanced setup** when you need a precise cupboard layout, permission approvals, MCP servers, Claude plugin directories, token warnings, or cost settings. These options remain available later through **Configure**.

## What onboarding does

Onboarding is read-only. It records:

- architecture and project boundaries;
- coding conventions;
- relevant test and validation commands;
- important files and directories; and
- risks or sensitive areas.

For a cupboard, the platform stores this context per repository and adds a summary of how the repositories relate. Tasks remain disabled until onboarding succeeds.

The Office may also find previous Claude Code or Codex sessions associated with the repository. It shows what it found and imports nothing unless you opt in. Approved imports use bounded transcript tails and feed useful information through the normal onboarding process; raw tool output is not copied into project context.

## Run the first task

Use reception when a request may involve more than one floor. Reception compares the request with all onboarded project summaries and routes scoped assignments to the owning floors.

For work that belongs only to the open floor, assign it directly there. The Manager & Tech Lead will:

1. analyze the request;
2. decide whether one or more workers are needed;
3. coordinate implementation and verification;
4. review the combined result; and
5. return the changes for your approval.

Implementation never pushes automatically. Review the working-tree diff, choose the hunks to publish, and explicitly confirm branch creation and pull-request publication.

## Next steps

- Learn the complete task lifecycle in [Platform concepts and workflows](platform-guide.md).
- Adjust executable paths, storage, models, and integrations in [Configuration](configuration.md).
- Understand checkpoints, backups, and the trust boundary in [Operations and security](operations.md).
