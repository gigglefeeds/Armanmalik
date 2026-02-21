# Jira Incremental Cache Reporter

This service solves the Jira reporting bottleneck by:
- doing a **one-time full sync** of project issues,
- then running **incremental sync** using `updated >= last_sync - lookback`,
- storing issues + changelog locally in a JSON file,
- serving reports quickly from local JSON cache (no repeated heavy Jira API scans).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set env vars:

```bash
export JIRA_EMAIL="you@example.com"
export JIRA_TOKEN="your_api_token"
export JIRA_DOMAIN="your-domain.atlassian.net"
export PROJECT_KEY="ESM"
```

Optional tuning:

```bash
export DATA_PATH="jira_cache.json"
export SYNC_PAGE_SIZE=100
export SYNC_LOOKBACK_MINUTES=120
```

## Run

```bash
python app.py
```

## Endpoints

### 1) Sync

`GET /sync` or `POST /sync`

- First run: full project sync.
- Next runs: incremental sync only changed/new tickets + their changelog.

Example cron (every 30 minutes):

```bash
*/30 * * * * curl -s http://localhost:5000/sync > /tmp/jira_sync.log 2>&1
```

### 2) Report

`GET /report?from=2026-01-01&to=2026-01-31&priority=high&breach_type=response`

Filters:
- `from`, `to` (created date)
- `ticket`
- `priority`
- `breach_type=response`

Returns JSON containing rows and breach summary.

## Why this is fast

- Jira pagination is handled properly (`startAt` + `maxResults` + `total`).
- We stop reloading all data every time.
- We only fetch changed issues based on `updated` timestamp.
- Local JSON cache reads are fast for monthly/audit reporting.
