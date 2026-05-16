# ptiles — Agent Instructions

## DapStack Kanban
- Project: ptiles
- Ticket prefix: See ~/.hermes/orchestrator/dapstack_profiles.yaml
- All new tasks must be created via the DapStack MCP tools (mcp_dapstack_*)
- Use the correct ticket prefix when referencing work in commits or PRs.

## Hermes Profile
- Preferred profile: See mapping in ~/.hermes/orchestrator/dapstack_profiles.yaml
- The orchestrator will automatically route work using this mapping.

## Free-Space Gate (when applicable)
- Before any large download, build, or data processing task, verify sufficient free space on the target disk (NAS preferred for large datasets).
- For PrePerc data work: always write to $PREPERC_DATA_ROOT (defaults to /mnt/unified/preperc-data).

## Commit Convention
- Prefix commits with the ticket number when possible, e.g. `[MDT-123] feat: ...`

Last updated: 2026-05-16
