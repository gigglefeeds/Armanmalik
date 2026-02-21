import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request
from requests.auth import HTTPBasicAuth

app = Flask(__name__)

# ---------- Config (set via env vars) ----------
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_TOKEN = os.getenv("JIRA_TOKEN", "")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN", "")
PROJECT_KEY = os.getenv("PROJECT_KEY", "")
DB_PATH = os.getenv("DB_PATH", "jira_cache.db")
SYNC_PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "100"))
SYNC_LOOKBACK_MINUTES = int(os.getenv("SYNC_LOOKBACK_MINUTES", "120"))

ISSUE_FIELDS = [
    "summary",
    "created",
    "updated",
    "reporter",
    "assignee",
    "priority",
    "status",
    "resolutiondate",
    "customfield_10010",
    "customfield_10941",
    "customfield_10336",
    "customfield_10334",
    "customfield_10232",
    "customfield_10342",
    "customfield_10343",
]


def jira_auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)


def jira_base_url() -> str:
    return f"https://{JIRA_DOMAIN}/rest/api/3"


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS issues (
                issue_key TEXT PRIMARY KEY,
                project_key TEXT,
                created TEXT,
                updated TEXT,
                priority TEXT,
                status TEXT,
                summary TEXT,
                reporter TEXT,
                assignee TEXT,
                complaint_time TEXT,
                resolved_by TEXT,
                channel TEXT,
                customer_name TEXT,
                resolution_friendly TEXT,
                issue_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS changelog (
                issue_key TEXT NOT NULL,
                history_id TEXT NOT NULL,
                created TEXT,
                author TEXT,
                items_json TEXT NOT NULL,
                PRIMARY KEY(issue_key, history_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_successful_sync TEXT
            )
            """
        )
        con.execute("INSERT OR IGNORE INTO sync_state (id, last_successful_sync) VALUES (1, NULL)")
        con.commit()


def get_last_sync() -> Optional[str]:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute("SELECT last_successful_sync FROM sync_state WHERE id=1").fetchone()
    return row[0] if row and row[0] else None


def set_last_sync(ts: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("UPDATE sync_state SET last_successful_sync=? WHERE id=1", (ts,))
        con.commit()


def parse_jira_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def fmt_hhmm(delta_hours: float) -> str:
    total_minutes = max(0, int(delta_hours * 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def get_allowed_response_hours(priority_name: str) -> float:
    return {"highest": 0.5, "high": 1, "medium": 2, "low": 2, "lowest": 2}.get(priority_name.lower(), 2)


def get_assigned_resolution_sla(priority_name: str) -> int:
    return {"highest": 4, "high": 8, "medium": 32, "low": 40, "lowest": 40}.get(priority_name.lower(), 40)


def fetch_issues_updated_since(updated_since: Optional[str]) -> List[Dict[str, Any]]:
    all_issues: List[Dict[str, Any]] = []
    start_at = 0

    if updated_since:
        jql = f'project = {PROJECT_KEY} AND updated >= "{updated_since}" ORDER BY updated ASC'
    else:
        jql = f"project = {PROJECT_KEY} ORDER BY created ASC"

    while True:
        payload = {
            "jql": jql,
            "fields": ISSUE_FIELDS,
            "startAt": start_at,
            "maxResults": SYNC_PAGE_SIZE,
        }
        resp = requests.post(
            f"{jira_base_url()}/search/jql",
            auth=jira_auth(),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("issues", [])
        all_issues.extend(batch)

        start_at += len(batch)
        total = data.get("total", 0)
        if start_at >= total or not batch:
            break

    return all_issues


def fetch_issue_changelog(issue_key: str) -> List[Dict[str, Any]]:
    start_at = 0
    all_histories: List[Dict[str, Any]] = []

    while True:
        resp = requests.get(
            f"{jira_base_url()}/issue/{issue_key}/changelog",
            auth=jira_auth(),
            headers={"Accept": "application/json"},
            params={"startAt": start_at, "maxResults": 100},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        values = data.get("values", [])
        all_histories.extend(values)

        start_at += len(values)
        total = data.get("total", 0)
        if start_at >= total or not values:
            break

    return all_histories


def upsert_issue_and_changelog(issue: Dict[str, Any], histories: List[Dict[str, Any]]) -> None:
    f = issue.get("fields", {})
    row = (
        issue.get("key"),
        PROJECT_KEY,
        f.get("created"),
        f.get("updated"),
        (f.get("priority") or {}).get("name", ""),
        (f.get("status") or {}).get("name", ""),
        f.get("summary", ""),
        (f.get("reporter") or {}).get("displayName", ""),
        (f.get("assignee") or {}).get("displayName", ""),
        f.get("customfield_10941", ""),
        (f.get("customfield_10336") or {}).get("displayName", ""),
        (f.get("customfield_10334") or {}).get("value", ""),
        f.get("customfield_10232", ""),
        (f.get("customfield_10343") or {}).get("friendly", ""),
        json.dumps(issue, ensure_ascii=False),
        datetime.now(timezone.utc).isoformat(),
    )

    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            INSERT INTO issues (
                issue_key, project_key, created, updated, priority, status, summary,
                reporter, assignee, complaint_time, resolved_by, channel, customer_name,
                resolution_friendly, issue_json, synced_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(issue_key) DO UPDATE SET
                project_key=excluded.project_key,
                created=excluded.created,
                updated=excluded.updated,
                priority=excluded.priority,
                status=excluded.status,
                summary=excluded.summary,
                reporter=excluded.reporter,
                assignee=excluded.assignee,
                complaint_time=excluded.complaint_time,
                resolved_by=excluded.resolved_by,
                channel=excluded.channel,
                customer_name=excluded.customer_name,
                resolution_friendly=excluded.resolution_friendly,
                issue_json=excluded.issue_json,
                synced_at=excluded.synced_at
            """,
            row,
        )

        for h in histories:
            con.execute(
                """
                INSERT INTO changelog(issue_key, history_id, created, author, items_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_key, history_id) DO UPDATE SET
                  created=excluded.created,
                  author=excluded.author,
                  items_json=excluded.items_json
                """,
                (
                    issue.get("key"),
                    str(h.get("id")),
                    h.get("created"),
                    (h.get("author") or {}).get("displayName", ""),
                    json.dumps(h.get("items", []), ensure_ascii=False),
                ),
            )

        con.commit()


def compute_workflow_steps(issue_key: str) -> Dict[str, str]:
    steps = {"pending": "", "pending_on_customer": "", "resolved": "", "pending_to_close": ""}
    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute(
            "SELECT created, items_json FROM changelog WHERE issue_key=? ORDER BY created ASC",
            (issue_key,),
        ).fetchall()

    for created, items_json in rows:
        items = json.loads(items_json or "[]")
        for item in items:
            if (item.get("field") or "").lower() != "status":
                continue
            to_val = (item.get("toString") or "").lower()
            if to_val == "pending" and not steps["pending"]:
                steps["pending"] = created
            elif to_val == "pending on customer" and not steps["pending_on_customer"]:
                steps["pending_on_customer"] = created
            elif to_val == "resolved" and not steps["resolved"]:
                steps["resolved"] = created
            elif to_val == "pending to close" and not steps["pending_to_close"]:
                steps["pending_to_close"] = created

    return steps


def sync_now() -> Dict[str, Any]:
    init_db()
    now_utc = datetime.now(timezone.utc)
    last_sync = get_last_sync()
    if last_sync:
        last_dt = parse_jira_time(last_sync)
        if last_dt is None:
            # isoformat from our DB
            last_dt = datetime.fromisoformat(last_sync)
        start_dt = last_dt - timedelta(minutes=SYNC_LOOKBACK_MINUTES)
        updated_since = start_dt.strftime("%Y-%m-%d %H:%M")
    else:
        updated_since = None

    issues = fetch_issues_updated_since(updated_since)
    for issue in issues:
        key = issue.get("key")
        histories = fetch_issue_changelog(key)
        upsert_issue_and_changelog(issue, histories)

    set_last_sync(now_utc.isoformat())
    return {
        "synced_issues": len(issues),
        "last_sync": now_utc.isoformat(),
        "mode": "incremental" if last_sync else "full",
    }


@app.route("/sync", methods=["POST", "GET"])
def sync_endpoint():
    try:
        result = sync_now()
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/report", methods=["GET"])
def report():
    init_db()
    start = request.args.get("from", "").strip()
    end = request.args.get("to", "").strip()
    ticket = request.args.get("ticket", "").strip().upper()
    priority_filter = request.args.get("priority", "").strip().lower()
    breach_type = request.args.get("breach_type", "").strip().lower()

    where = ["1=1"]
    params: List[Any] = []

    if ticket:
        where.append("issue_key = ?")
        params.append(ticket)
    if start:
        where.append("date(created) >= date(?)")
        params.append(start)
    if end:
        where.append("date(created) <= date(?)")
        params.append(end)
    if priority_filter:
        where.append("lower(priority) = ?")
        params.append(priority_filter)

    query = f"SELECT issue_key, summary, created, reporter, assignee, priority, status, resolved_by, channel, customer_name, complaint_time, resolution_friendly FROM issues WHERE {' AND '.join(where)} ORDER BY created ASC"

    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute(query, params).fetchall()

    result_rows = []
    total_response_breach = 0

    for row in rows:
        (
            issue_key,
            summary,
            created,
            reporter,
            assignee,
            priority,
            status,
            resolved_by,
            channel,
            customer_name,
            complaint_time,
            resolution_friendly,
        ) = row

        created_dt = parse_jira_time(created)
        complaint_dt = parse_jira_time(complaint_time)
        response_hours = ""
        response_breach = ""
        if created_dt and complaint_dt:
            delta_h = (created_dt - complaint_dt).total_seconds() / 3600
            response_hours = fmt_hhmm(delta_h)
            response_breach = "Yes" if delta_h > get_allowed_response_hours(priority or "") else "No"

        if breach_type == "response" and response_breach != "Yes":
            continue

        if response_breach == "Yes":
            total_response_breach += 1

        steps = compute_workflow_steps(issue_key)
        result_rows.append(
            {
                "key": issue_key,
                "summary": summary,
                "created": created,
                "reporter": reporter,
                "assignee": assignee,
                "priority": priority,
                "status": status,
                "resolved_by": resolved_by,
                "channel": channel,
                "customer_name": customer_name,
                "complaint_time": complaint_time,
                "response_time": response_hours,
                "response_breach": response_breach,
                "assigned_response_sla": get_allowed_response_hours(priority or ""),
                "resolution_sla_hours": get_assigned_resolution_sla(priority or ""),
                "resolution_time": resolution_friendly,
                "pending": steps["pending"],
                "pending_on_customer": steps["pending_on_customer"],
                "resolved": steps["resolved"],
                "pending_to_close": steps["pending_to_close"],
            }
        )

    return jsonify(
        {
            "count": len(result_rows),
            "breach_summary": {"response_breach": total_response_breach},
            "rows": result_rows,
        }
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
