## Jira full + incremental dump

This repo now provides `jira_sync.py` to solve the 100-record limit and slow/manual monthly reporting workflows.

### What it does
- Pulls all issues for one or more projects using Jira pagination (`startAt`, `maxResults`).
- Fetches **all project fields** from Jira and stores them in the JSON dump.
- Fetches each issue changelog (paginated) and stores compact history.
- Supports **full load** (first run) and **incremental load** (every 30 minutes, or any interval).
- Incremental mode pulls only tickets updated since last sync (with lookback buffer).

### Setup
```bash
export JIRA_EMAIL="your-email"
export JIRA_TOKEN="your-token"
export JIRA_DOMAIN="your-domain.atlassian.net"
```

### Full load
```bash
python jira_sync.py sync --projects ESM,ABC --full --status-only-changelog
```

### Incremental load (example every 30 mins)
```bash
python jira_sync.py sync --projects ESM,ABC --interval-minutes 30 --status-only-changelog
```

### Output
- Data store: `data/jira_store.json`
- State file: `data/jira_state.json`

Use cron to run incremental command every 30 minutes.
