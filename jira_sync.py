#!/usr/bin/env python3
"""Fast Jira data sync (full + incremental) with changelog support.

Usage examples:
  python jira_sync.py sync --projects ESM,ABC --full
  python jira_sync.py sync --projects ESM --interval-minutes 30

Configuration via environment variables:
  JIRA_EMAIL, JIRA_TOKEN, JIRA_DOMAIN
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

DEFAULT_STORE = Path("data/jira_store.json")
DEFAULT_STATE = Path("data/jira_state.json")


class JiraClient:
    def __init__(self, domain: str, email: str, token: str, timeout: int = 30) -> None:
        self.base = f"https://{domain}/rest/api/3"
        self.auth = HTTPBasicAuth(email, token)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base}{path}"
        resp = self.session.request(method, url, auth=self.auth, timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code} at {path}: {resp.text[:300]}")
        return resp.json() if resp.text else {}

    def fetch_fields(self) -> list[dict[str, Any]]:
        return self._request("GET", "/field")

    def search_issues(self, jql: str, fields: list[str] | None = None, batch: int = 100) -> list[dict[str, Any]]:
        start_at = 0
        all_issues: list[dict[str, Any]] = []
        while True:
            payload: dict[str, Any] = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": batch,
                "fields": fields or ["*all"],
            }
            data = self._request("POST", "/search", data=json.dumps(payload))
            issues = data.get("issues", [])
            all_issues.extend(issues)
            total = data.get("total", 0)
            start_at += len(issues)
            if start_at >= total or not issues:
                break
        return all_issues

    def fetch_issue_changelog(self, issue_key: str, batch: int = 100) -> list[dict[str, Any]]:
        start_at = 0
        all_histories: list[dict[str, Any]] = []
        while True:
            data = self._request("GET", f"/issue/{issue_key}/changelog?startAt={start_at}&maxResults={batch}")
            histories = data.get("values", [])
            all_histories.extend(histories)
            start_at += len(histories)
            if start_at >= data.get("total", 0) or not histories:
                break
        return all_histories


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def now_utc_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def compact_changelog(histories: list[dict[str, Any]], status_only: bool) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for h in histories:
        items = h.get("items", [])
        if status_only:
            items = [x for x in items if x.get("field") == "status"]
            if not items:
                continue
        compact.append(
            {
                "id": h.get("id"),
                "created": h.get("created"),
                "author": (h.get("author") or {}).get("displayName"),
                "items": [
                    {
                        "field": x.get("field"),
                        "from": x.get("fromString"),
                        "to": x.get("toString"),
                    }
                    for x in items
                ],
            }
        )
    return compact


def sync(
    client: JiraClient,
    projects: list[str],
    full: bool,
    interval_minutes: int,
    workers: int,
    status_only_changelog: bool,
    store_path: Path,
    state_path: Path,
) -> None:
    state = load_json(state_path, {"last_sync": None, "projects": []})
    store = load_json(store_path, {"synced_at": None, "fields": [], "issues": {}})

    store["fields"] = client.fetch_fields()
    since = None
    if not full and state.get("last_sync"):
        since_dt = parse_iso(state["last_sync"]) - timedelta(minutes=interval_minutes)
        since = since_dt.strftime("%Y-%m-%d %H:%M")

    project_jql = ",".join(projects)
    jql = f"project in ({project_jql})"
    if since:
        jql += f' AND updated >= "{since}"'
    jql += " ORDER BY updated ASC"

    issues = client.search_issues(jql=jql, fields=["*all", "updated", "created", "status"])

    keys = [issue["key"] for issue in issues]
    print(f"[sync] issues to process: {len(keys)}")

    changelog_map: dict[str, list[dict[str, Any]]] = {}
    if keys:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(client.fetch_issue_changelog, key): key for key in keys}
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    raw = fut.result()
                    changelog_map[key] = compact_changelog(raw, status_only=status_only_changelog)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] changelog failed for {key}: {exc}")

    for issue in issues:
        key = issue["key"]
        store["issues"][key] = {
            "key": key,
            "updated": issue["fields"].get("updated"),
            "created": issue["fields"].get("created"),
            "fields": issue["fields"],
            "changelog": changelog_map.get(key, store["issues"].get(key, {}).get("changelog", [])),
        }

    store["synced_at"] = now_utc_str()
    save_json(store_path, store)

    state["last_sync"] = now_utc_str()
    state["projects"] = projects
    save_json(state_path, state)

    print(f"[ok] store: {store_path} | total cached issues: {len(store['issues'])}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Jira full/incremental sync")
    p.add_argument("action", choices=["sync"], help="Only sync is supported")
    p.add_argument("--projects", required=True, help="Comma-separated project keys, e.g. ESM,ABC")
    p.add_argument("--full", action="store_true", help="Run full load. If absent, incremental mode is used")
    p.add_argument("--interval-minutes", type=int, default=30, help="Lookback window for incremental sync")
    p.add_argument("--workers", type=int, default=8, help="Parallel workers for changelog fetch")
    p.add_argument("--status-only-changelog", action="store_true", help="Keep only status transitions in changelog")
    p.add_argument("--store", default=str(DEFAULT_STORE), help="Output JSON store path")
    p.add_argument("--state", default=str(DEFAULT_STATE), help="Sync state file path")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_TOKEN")
    domain = os.getenv("JIRA_DOMAIN")

    if not all([email, token, domain]):
        raise SystemExit("Missing env vars: JIRA_EMAIL, JIRA_TOKEN, JIRA_DOMAIN")

    projects = [x.strip().upper() for x in args.projects.split(",") if x.strip()]
    if not projects:
        raise SystemExit("At least one project key is required")

    start = time.perf_counter()
    client = JiraClient(domain=domain, email=email, token=token)
    sync(
        client=client,
        projects=projects,
        full=args.full,
        interval_minutes=args.interval_minutes,
        workers=args.workers,
        status_only_changelog=args.status_only_changelog,
        store_path=Path(args.store),
        state_path=Path(args.state),
    )
    print(f"[done] elapsed={time.perf_counter() - start:.2f}s")


if __name__ == "__main__":
    main()
