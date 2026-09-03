# The Office documentation

The Office coordinates local Claude Code and Codex agents across Git repositories. These guides cover the platform from first launch through day-to-day operation and recovery.

## Start here

- [Getting started](getting-started.md) explains the requirements, startup process, and first project onboarding.
- [Platform concepts and workflows](platform-guide.md) explains how floors, leads, workers, reception, reports, specifications, review, and publishing fit together.
- [Configuration reference](configuration.md) lists runtime settings and project-level controls.
- [Operations and security](operations.md) covers storage, backups, recovery, troubleshooting, and safe deployment.

## Common tasks

| I want to… | Read |
| --- | --- |
| Add my first repository | [Create a floor](getting-started.md#create-a-floor) |
| Coordinate a task across repositories | [Reception and routing](platform-guide.md#reception-and-routing) |
| Ask about a codebase without changing it | [Questions and reports](platform-guide.md#questions-and-reports) |
| Work from a phased specification | [Specification trackers](platform-guide.md#specification-trackers) |
| Review or undo agent changes | [Review and recovery](platform-guide.md#review-and-recovery) |
| Change where data is stored | [Data and retention](configuration.md#data-and-retention) |
| Configure models, MCP, or plugins | [Agent configuration](configuration.md#agent-configuration) |
| Diagnose a failed run | [Troubleshooting](operations.md#troubleshooting) |
| Back up or export platform state | [Persistence and backups](operations.md#persistence-and-backups) |

## Scope

The documentation describes The Office as a local, single-user platform. The server runs agent CLIs and Git commands with the permissions of the current operating-system user. It is not designed to be hosted as an unauthenticated shared service.
