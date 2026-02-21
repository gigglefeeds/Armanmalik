import json
import os
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
DATA_PATH = os.getenv("DATA_PATH", "jira_cache.json")
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


def load_store() -> Dict[str, Any]:
    if not os.path.exists(DATA_PATH):
        return {"sync_state": {"last_successful_sync": None}, "issues": {}, "changelog": {}}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("sync_state", {"last_successful_sync": None})
    data.setdefault("issues", {})
    data.setdefault("changelog", {})
    return data


def save_store(data: Dict[str, Any]) -> None:
    temp_path = f"{DATA_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(temp_path, DATA_PATH)


def get_last_sync(data: Dict[str, Any]) -> Optional[str]:
    return (data.get("sync_state") or {}).get("last_successful_sync")


def set_last_sync(data: Dict[str, Any], ts: str) -> None:
    data.setdefault("sync_state", {})["last_successful_sync"] = ts


def parse_jira_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
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


def build_issue_record(issue: Dict[str, Any]) -> Dict[str, Any]:
    f = issue.get("fields", {})
    return {
        "issue_key": issue.get("key", ""),
        "project_key": PROJECT_KEY,
        "created": f.get("created", ""),
        "updated": f.get("updated", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "summary": f.get("summary", ""),
        "reporter": (f.get("reporter") or {}).get("displayName", ""),
        "assignee": (f.get("assignee") or {}).get("displayName", ""),
        "complaint_time": f.get("customfield_10941", ""),
        "resolved_by": (f.get("customfield_10336") or {}).get("displayName", ""),
        "channel": (f.get("customfield_10334") or {}).get("value", ""),
        "customer_name": f.get("customfield_10232", ""),
        "resolution_friendly": (f.get("customfield_10343") or {}).get("friendly", ""),
        "issue_json": issue,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_issue_and_changelog(data: Dict[str, Any], issue: Dict[str, Any], histories: List[Dict[str, Any]]) -> None:
    issue_key = issue.get("key", "")
    data.setdefault("issues", {})[issue_key] = build_issue_record(issue)

    issue_changelog: Dict[str, Dict[str, Any]] = data.setdefault("changelog", {}).setdefault(issue_key, {})
    for h in histories:
        history_id = str(h.get("id", ""))
        issue_changelog[history_id] = {
            "created": h.get("created", ""),
            "author": (h.get("author") or {}).get("displayName", ""),
            "items": h.get("items", []),
        }


def compute_workflow_steps(data: Dict[str, Any], issue_key: str) -> Dict[str, str]:
    steps = {"pending": "", "pending_on_customer": "", "resolved": "", "pending_to_close": ""}
    logs = list((data.get("changelog") or {}).get(issue_key, {}).values())
    logs.sort(key=lambda x: x.get("created", ""))

    for h in logs:
        created = h.get("created", "")
        for item in h.get("items", []):
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
    data = load_store()
    now_utc = datetime.now(timezone.utc)
    last_sync = get_last_sync(data)

    if last_sync:
        last_dt = parse_jira_time(last_sync)
        if last_dt is None:
            raise ValueError("Invalid last_successful_sync format in JSON store")
        start_dt = last_dt - timedelta(minutes=SYNC_LOOKBACK_MINUTES)
        updated_since = start_dt.strftime("%Y-%m-%d %H:%M")
    else:
        updated_since = None

    issues = fetch_issues_updated_since(updated_since)
    for issue in issues:
        key = issue.get("key")
        histories = fetch_issue_changelog(key)
        upsert_issue_and_changelog(data, issue, histories)

    set_last_sync(data, now_utc.isoformat())
    save_store(data)

    return {
        "synced_issues": len(issues),
        "last_sync": now_utc.isoformat(),
        "mode": "incremental" if last_sync else "full",
        "data_file": DATA_PATH,
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
    data = load_store()

    start = request.args.get("from", "").strip()
    end = request.args.get("to", "").strip()
    ticket = request.args.get("ticket", "").strip().upper()
    priority_filter = request.args.get("priority", "").strip().lower()
    breach_type = request.args.get("breach_type", "").strip().lower()

    all_issues = list((data.get("issues") or {}).values())
    all_issues.sort(key=lambda x: x.get("created", ""))

    result_rows = []
    total_response_breach = 0

    for rec in all_issues:
        issue_key = rec.get("issue_key", "")
        if ticket and issue_key != ticket:
            continue

        created = rec.get("created", "")
        created_dt = parse_jira_time(created)
        if start and created_dt and created_dt.date() < datetime.strptime(start, "%Y-%m-%d").date():
            continue
        if end and created_dt and created_dt.date() > datetime.strptime(end, "%Y-%m-%d").date():
            continue

        priority = rec.get("priority", "")
        if priority_filter and priority.lower() != priority_filter:
            continue

        complaint_time = rec.get("complaint_time", "")
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

        steps = compute_workflow_steps(data, issue_key)
        result_rows.append(
            {
                "key": issue_key,
                "summary": rec.get("summary", ""),
                "created": created,
                "reporter": rec.get("reporter", ""),
                "assignee": rec.get("assignee", ""),
                "priority": priority,
                "status": rec.get("status", ""),
                "resolved_by": rec.get("resolved_by", ""),
                "channel": rec.get("channel", ""),
                "customer_name": rec.get("customer_name", ""),
                "complaint_time": complaint_time,
                "response_time": response_hours,
                "response_breach": response_breach,
                "assigned_response_sla": get_allowed_response_hours(priority or ""),
                "resolution_sla_hours": get_assigned_resolution_sla(priority or ""),
                "resolution_time": rec.get("resolution_friendly", ""),
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
            "data_file": DATA_PATH,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
