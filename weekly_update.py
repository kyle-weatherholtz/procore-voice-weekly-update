#!/usr/bin/env python3
"""
Procore Voice Weekly Update Automation
Collects data from GitHub, Jira, Confluence, Gong (Snowflake), and Granola,
synthesizes an LT update via Claude, then prepends it to the standing Confluence page.

Usage:
  python weekly_update.py                        # Full run — write to Confluence
  python weekly_update.py --dry-run              # Fetch + synthesize, print only
  python weekly_update.py --since 2026-04-07     # Override 7-day lookback
  python weekly_update.py --skip-gong            # Skip Snowflake/Gong
  python weekly_update.py --skip-granola         # Skip Granola MCP
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.json and extract GitHub PAT from ~/.cursor/mcp.json."""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        sys.exit(
            "config.json not found. Copy config.example.json → config.json and fill in your tokens."
        )
    with open(config_path) as f:
        config = json.load(f)

    # GitHub PAT lives in ~/.cursor/mcp.json under mcpServers.github.env
    mcp_path = Path.home() / ".cursor" / "mcp.json"
    if mcp_path.exists():
        with open(mcp_path) as f:
            mcp = json.load(f)
        pat = (
            mcp.get("mcpServers", {})
            .get("github", {})
            .get("env", {})
            .get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        )
        if pat:
            config["github_pat"] = pat
        else:
            print("WARNING: GITHUB_PERSONAL_ACCESS_TOKEN not found in ~/.cursor/mcp.json — GitHub data will be skipped.", file=sys.stderr)
    else:
        print("WARNING: ~/.cursor/mcp.json not found — GitHub data will be skipped.", file=sys.stderr)

    return config


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def fetch_github_data(config: dict, since: datetime) -> dict:
    """Fetch merged PRs and recent commits from the voice-ios repo."""
    pat = config.get("github_pat")
    if not pat:
        return {"prs": [], "commits": []}

    repo = config.get("github_repo", "procore/voice-ios")
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Merged PRs
    prs = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 50,
                "page": page,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for pr in batch:
            if pr.get("merged_at") and pr["merged_at"] >= since_iso:
                prs.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "merged_at": pr["merged_at"],
                    "user": pr["user"]["login"],
                    "body": (pr.get("body") or "")[:500],
                    "url": pr["html_url"],
                })
            elif pr.get("updated_at", "") < since_iso:
                # PRs are sorted by updated desc; once we pass the window, stop
                batch = []  # signal to break outer loop
                break
        if not batch:
            break
        page += 1

    # Recent commits on main/master
    commits = []
    for branch in ["main", "master"]:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/commits",
            headers=headers,
            params={"since": since_iso, "per_page": 50, "sha": branch},
            timeout=30,
        )
        if resp.status_code == 200:
            for c in resp.json():
                commits.append({
                    "sha": c["sha"][:7],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                })
            break  # got results from this branch, don't try master

    print(f"  GitHub: {len(prs)} merged PRs, {len(commits)} commits")
    return {"prs": prs, "commits": commits}


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def fetch_jira_data(config: dict, since: datetime) -> list:
    """Fetch closed/transitioned Jira tickets from FSAD + MARCH boards."""
    email = config["atlassian_email"]
    token = config["atlassian_api_token"]
    base = config["atlassian_base_url"]
    projects = config.get("jira_projects", ["FSAD", "MARCH"])

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}

    since_str = since.strftime("%Y-%m-%d")
    project_filter = ", ".join(f'"{p}"' for p in projects)
    jql = (
        f'project in ({project_filter}) '
        f'AND updated >= "{since_str}" '
        f'AND status in ("Done", "Closed", "Released", "Resolved") '
        f'ORDER BY updated DESC'
    )

    tickets = []
    start = 0
    while True:
        resp = requests.get(
            f"{base}/rest/api/3/search",
            headers=headers,
            params={"jql": jql, "startAt": start, "maxResults": 50,
                    "fields": "summary,status,assignee,priority,resolutiondate,labels,parent"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", [])
        if not issues:
            break
        for issue in issues:
            f = issue["fields"]
            tickets.append({
                "key": issue["key"],
                "summary": f.get("summary", ""),
                "status": f.get("status", {}).get("name", ""),
                "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                "priority": f.get("priority", {}).get("name", ""),
                "labels": f.get("labels", []),
                "url": f"{base}/browse/{issue['key']}",
            })
        start += len(issues)
        if start >= data.get("total", 0):
            break

    print(f"  Jira: {len(tickets)} closed/transitioned tickets")
    return tickets


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

def fetch_confluence_context(config: dict) -> dict:
    """GET the current Confluence page body and version for context + prepend."""
    email = config["atlassian_email"]
    token = config["atlassian_api_token"]
    base = config["atlassian_base_url"]
    page_id = config["confluence_page_id"]

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}

    resp = requests.get(
        f"{base}/wiki/rest/api/content/{page_id}",
        headers=headers,
        params={"expand": "body.storage,version,title"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "title": data["title"],
        "version": data["version"]["number"],
        "body": data["body"]["storage"]["value"],
    }


# ---------------------------------------------------------------------------
# Gong via Snowflake
# ---------------------------------------------------------------------------

def build_gong_sql(since: datetime, warehouse: str) -> str:
    keywords = [
        "voice", "field ai", "procore voice", "speech", "transcription",
        "dictation", "voice assistant", "field assistant",
    ]
    conditions = " OR\n    ".join(
        f"LOWER(cl.CALL_SPOTLIGHT_BRIEF) LIKE '%{kw}%'\n    "
        f"OR LOWER(cl.TITLE) LIKE '%{kw}%'"
        for kw in keywords
    )
    since_str = since.strftime("%Y-%m-%d")
    return f"""USE SECONDARY ROLES ALL;
USE WAREHOUSE {warehouse};
ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 60;
SELECT DISTINCT
  cl.TITLE AS CALL_TITLE,
  TO_CHAR(DATE(c.CONVERSATION_DATETIME), 'YYYY-MM-DD') AS CALL_DATE,
  c.CONVERSATION_ID,
  'https://us-47049.app.gong.io/call?id=' || c.CONVERSATION_ID AS GONG_URL,
  COALESCE(cl.CALL_SPOTLIGHT_BRIEF, '') AS CALL_SPOTLIGHT_BRIEF,
  COALESCE(cl.CALL_SPOTLIGHT_NEXT_STEPS, '') AS CALL_SPOTLIGHT_NEXT_STEPS,
  sa.NAME AS ACCOUNT_NAME,
  sa.SALES_TEAM_SEGMENT_C AS ACCOUNT_SEGMENT
FROM PROCORE_IT.GONG_DATA_CLOUD_PREP.GONG_CONVERSATIONS_PREP c
JOIN PROCORE_IT.GONG_DATA_CLOUD_PREP.GONG_CALLS_PREP cl
  ON c.CONVERSATION_KEY = cl.CONVERSATION_KEY
LEFT JOIN PROCORE_IT.GONG_DATA_CLOUD_PREP.GONG_CONVERSATION_CONTEXTS_PREP cc
  ON c.CONVERSATION_KEY = cc.CONVERSATION_KEY AND cc.OBJECT_TYPE = 'Account'
LEFT JOIN PROCORE_IT.SALESFORCE.ACCOUNT sa
  ON cc.OBJECT_ID = sa.ID
WHERE c.IS_DELETED = FALSE
  AND cl.STATUS = 'COMPLETED'
  AND DATE(c.CONVERSATION_DATETIME) >= '{since_str}'
  AND c.CONVERSATION_DATETIME <= CURRENT_TIMESTAMP()
  AND (
    {conditions}
  )
ORDER BY CALL_DATE DESC
LIMIT 200;"""


def fetch_gong_data(config: dict, since: datetime) -> list:
    """Run snow sql to fetch Gong call summaries mentioning voice/field AI."""
    connection = config.get("snowflake_connection", "Snowflake")
    warehouse = config.get("snowflake_warehouse", "PNT_PRODUCT_MGMT_WH")
    sql = build_gong_sql(since, warehouse)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(sql)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["snow", "sql", "-f", tmp_path, "--format", "json", "-c", connection],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        os.unlink(tmp_path)

    if result.returncode != 0:
        print(f"  Gong/Snowflake ERROR: {result.stderr[:300]}", file=sys.stderr)
        return []

    try:
        result_sets = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("  Gong/Snowflake: could not parse JSON output", file=sys.stderr)
        return []

    # Multi-statement queries return array of result sets; find the actual data rows
    sets = result_sets if isinstance(result_sets, list) else [result_sets]
    rows = []
    for rs in reversed(sets):
        if isinstance(rs, list) and rs and "status" not in rs[0]:
            rows = rs
            break

    calls = [
        {
            "title": r.get("CALL_TITLE", ""),
            "date": r.get("CALL_DATE", ""),
            "account": r.get("ACCOUNT_NAME", ""),
            "segment": r.get("ACCOUNT_SEGMENT", ""),
            "brief": r.get("CALL_SPOTLIGHT_BRIEF", "")[:600],
            "next_steps": r.get("CALL_SPOTLIGHT_NEXT_STEPS", "")[:300],
            "url": r.get("GONG_URL", ""),
        }
        for r in rows
    ]
    print(f"  Gong: {len(calls)} relevant calls")
    return calls


# ---------------------------------------------------------------------------
# Granola via MCP JSON-RPC
# ---------------------------------------------------------------------------

GRANOLA_MCP_URL = "https://mcp.granola.ai/mcp"


def _mcp_rpc(proc, method: str, params: dict, req_id: int) -> dict:
    """Send a JSON-RPC request over stdio and read the response."""
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
    proc.stdin.write(payload)
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP process closed stdout")
    return json.loads(line)


def fetch_granola_notes(since: datetime) -> list:
    """Spawn npx mcp-remote, list notes from the past week via MCP JSON-RPC."""
    notes = []
    try:
        proc = subprocess.Popen(
            ["npx", "--yes", "mcp-remote", GRANOLA_MCP_URL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("  Granola: npx not found — skipping", file=sys.stderr)
        return []

    try:
        # Initialize
        init_resp = _mcp_rpc(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "procore-voice-weekly-update", "version": "1.0"},
        }, 1)
        if "error" in init_resp:
            print(f"  Granola MCP init error: {init_resp['error']}", file=sys.stderr)
            return []

        # Send initialized notification
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        # List available tools
        tools_resp = _mcp_rpc(proc, "tools/list", {}, 2)
        tools = {t["name"] for t in tools_resp.get("result", {}).get("tools", [])}

        # Try list_notes or search_notes — tool name varies by Granola MCP version
        list_tool = next((t for t in ["list_notes", "get_notes", "search_notes"] if t in tools), None)
        if not list_tool:
            print(f"  Granola: no note-listing tool found (available: {tools})", file=sys.stderr)
            return []

        since_iso = since.isoformat()
        list_resp = _mcp_rpc(proc, "tools/call", {
            "name": list_tool,
            "arguments": {"since": since_iso, "limit": 50},
        }, 3)

        result = list_resp.get("result", {})
        content_items = result.get("content", [])
        req_id = 4

        for item in content_items:
            text = item.get("text", "")
            try:
                note_list = json.loads(text)
                if not isinstance(note_list, list):
                    note_list = [note_list]
            except (json.JSONDecodeError, TypeError):
                continue

            for note_meta in note_list:
                note_id = note_meta.get("id") or note_meta.get("noteId")
                title = note_meta.get("title", "Untitled")
                date = note_meta.get("date") or note_meta.get("created_at", "")

                # Fetch full content if there's a get_note tool
                content = note_meta.get("content", "")
                if not content and "get_note" in tools and note_id:
                    detail_resp = _mcp_rpc(proc, "tools/call", {
                        "name": "get_note",
                        "arguments": {"id": note_id},
                    }, req_id)
                    req_id += 1
                    for ci in detail_resp.get("result", {}).get("content", []):
                        if ci.get("type") == "text":
                            content = ci["text"][:1000]
                            break

                notes.append({"title": title, "date": date, "content": content[:1000]})

    except Exception as e:
        print(f"  Granola: error — {e}", file=sys.stderr)
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"  Granola: {len(notes)} meeting notes")
    return notes


# ---------------------------------------------------------------------------
# Claude synthesis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a product manager writing a weekly update for Procore's Leadership Team (LT).
Your job is to synthesize raw data from multiple sources into a concise, outcome-focused update.

Privacy rules — strictly enforce:
- Reference companies by name only; remove individual customer names and titles
- Remove internal employee names in sensitive/HR/legal contexts
- Exclude content from private Slack DMs
- Exclude legal negotiation details or specific deal terms
- Keep: milestone status, engineering progress, business outcomes, product decisions appropriate for a broad internal audience

Tone: Direct, confident, outcome-first. No filler. No hedging.
"""

LT_UPDATE_FORMAT = """
Format the update exactly like this (use literal emoji characters, not codes):

---
Procore Voice: Update for Leadership ({date})

Milestone Tracker
⏳ [milestone name] (target date) - In Progress
✅ [milestone name] (target date) - Complete
➡️ [milestone name] (target date) - On track
⚠️ [milestone name] (target date) - At risk

[One or two sentence context paragraph on overall status]

Wins
- [Outcome-first bullet]. [Supporting detail or link if relevant.]
- ...

Blockers
[Either "No critical blockers at this time." or bullet list of real blockers]

In Progress
- [What is being worked on and why it matters]
- ...
---

Rules:
- Lead with milestone status changes — if a date moved or a milestone was hit, that is the lede
- Wins are outcome-first (not action-first)
- Blockers section is honest — name the risk even if no resolution yet
- In Progress covers work underway heading into next week
- No implementation detail (PR numbers, specific bug names) unless it shifts a milestone date
- If data is sparse for a section, write "Nothing significant this week." rather than making things up
"""


def synthesize_update(all_data: dict, config: dict, today: datetime) -> str:
    """Call Claude API to synthesize the LT update from all collected data."""
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    date_str = today.strftime("%B %-d")  # e.g. "April 18"

    data_block = f"""
## Raw data for the week ending {today.strftime('%Y-%m-%d')}

### GitHub — Merged PRs ({len(all_data['github']['prs'])} total)
{json.dumps(all_data['github']['prs'][:20], indent=2)}

### GitHub — Recent Commits ({len(all_data['github']['commits'])} total)
{json.dumps(all_data['github']['commits'][:20], indent=2)}

### Jira — Closed/Transitioned Tickets ({len(all_data['jira'])} total)
{json.dumps(all_data['jira'][:30], indent=2)}

### Gong Calls mentioning Voice/Field AI ({len(all_data['gong'])} total)
{json.dumps(all_data['gong'][:15], indent=2)}

### Granola Meeting Notes ({len(all_data['granola'])} total)
{json.dumps(all_data['granola'][:10], indent=2)}

### Confluence page context (existing content — for milestone continuity)
{all_data['confluence_context'][:3000]}
"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"{LT_UPDATE_FORMAT.format(date=date_str)}\n\nHere is the raw data:\n{data_block}",
            }
        ],
    )

    return message.content[0].text


# ---------------------------------------------------------------------------
# Confluence write
# ---------------------------------------------------------------------------

def _as_confluence_storage(markdown_text: str) -> str:
    """Wrap the markdown update in a Confluence storage format panel."""
    # Escape for XML inside Confluence storage format
    escaped = (
        markdown_text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    # Wrap each line to preserve formatting
    lines_html = "".join(f"<p>{line}</p>" if line.strip() else "<p></p>" for line in escaped.split("\n"))
    return f'<ac:structured-macro ac:name="panel"><ac:parameter ac:name="title">Week of {datetime.now().strftime("%B %-d, %Y")}</ac:parameter><ac:rich-text-body>{lines_html}</ac:rich-text-body></ac:structured-macro><hr/>'


def update_confluence_page(config: dict, new_section: str) -> None:
    """Prepend new_section to the Confluence page body and PUT the update."""
    email = config["atlassian_email"]
    token = config["atlassian_api_token"]
    base = config["atlassian_base_url"]
    page_id = config["confluence_page_id"]

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # GET current page
    resp = requests.get(
        f"{base}/wiki/rest/api/content/{page_id}",
        headers=headers,
        params={"expand": "body.storage,version,title"},
        timeout=30,
    )
    resp.raise_for_status()
    page = resp.json()
    current_body = page["body"]["storage"]["value"]
    current_version = page["version"]["number"]
    title = page["title"]

    # Prepend new section
    new_body = _as_confluence_storage(new_section) + "\n" + current_body

    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": current_version + 1},
        "body": {
            "storage": {
                "value": new_body,
                "representation": "storage",
            }
        },
    }

    put_resp = requests.put(
        f"{base}/wiki/rest/api/content/{page_id}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    put_resp.raise_for_status()
    print(f"  Confluence: page updated to version {current_version + 1}")


# ---------------------------------------------------------------------------
# Output archive
# ---------------------------------------------------------------------------

def save_output(text: str, today: datetime) -> Path:
    """Save generated update to output/YYYY-MM-DD.md."""
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    path = output_dir / f"{today.strftime('%Y-%m-%d')}.md"
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Procore Voice Weekly Update")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + synthesize, print to stdout. No Confluence write.")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Override lookback start date (default: 7 days ago)")
    parser.add_argument("--skip-gong", action="store_true",
                        help="Skip Snowflake/Gong (use if Okta session is expired)")
    parser.add_argument("--skip-granola", action="store_true",
                        help="Skip Granola MCP (use if auth has lapsed)")
    args = parser.parse_args()

    today = datetime.now(timezone.utc)
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        since = today - timedelta(days=7)

    print(f"Procore Voice Weekly Update — {today.strftime('%Y-%m-%d')}")
    print(f"Lookback: {since.strftime('%Y-%m-%d')} → {today.strftime('%Y-%m-%d')}")
    print()

    print("Loading config...")
    config = load_config()

    print("Fetching data sources...")

    github_data = fetch_github_data(config, since)

    jira_data = fetch_jira_data(config, since)

    confluence_ctx = fetch_confluence_context(config)
    print(f"  Confluence: loaded page '{confluence_ctx['title']}' (v{confluence_ctx['version']})")

    if args.skip_gong:
        print("  Gong: skipped (--skip-gong)")
        gong_data = []
    else:
        gong_data = fetch_gong_data(config, since)

    if args.skip_granola:
        print("  Granola: skipped (--skip-granola)")
        granola_data = []
    else:
        granola_data = fetch_granola_notes(since)

    print()
    print("Synthesizing update with Claude...")
    all_data = {
        "github": github_data,
        "jira": jira_data,
        "gong": gong_data,
        "granola": granola_data,
        "confluence_context": confluence_ctx["body"],
    }
    update_text = synthesize_update(all_data, config, today)

    # Always save locally
    saved_path = save_output(update_text, today)
    print(f"  Saved to: {saved_path}")
    print()

    if args.dry_run:
        print("=" * 70)
        print(update_text)
        print("=" * 70)
        print()
        print("DRY RUN — Confluence not updated.")
    else:
        print("Writing to Confluence...")
        update_confluence_page(config, update_text)
        print()
        print("Done.")


if __name__ == "__main__":
    main()
