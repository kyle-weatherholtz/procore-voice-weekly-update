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
import json
import os
import re
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
        done = False
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
                done = True
                break
        if done:
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
            break

    print(f"  GitHub: {len(prs)} merged PRs, {len(commits)} commits")
    return {"prs": prs, "commits": commits}


# ---------------------------------------------------------------------------
# Atlassian MCP (Jira + Confluence)
# ---------------------------------------------------------------------------

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"


class AtlassianMCP:
    """Context manager that wraps a single npx mcp-remote subprocess for all Atlassian calls."""

    def __init__(self):
        self.proc = None
        self._req_id = 1
        self.cloud_id = None

    def _rpc(self, method: str, params: dict) -> dict:
        req_id = self._req_id
        self._req_id += 1
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
        self.proc.stdin.write(msg)
        self.proc.stdin.flush()
        # Read lines until we find the response for our req_id (skip non-JSON / notifications)
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("Atlassian MCP process closed stdout unexpectedly")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
                if resp.get("id") == req_id:
                    return resp
                # Otherwise it's a notification or a different response — keep reading
            except json.JSONDecodeError:
                continue

    def call(self, tool_name: str, arguments: dict) -> str:
        """Call a tool and return the text of the first content item."""
        resp = self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if "error" in resp:
            raise RuntimeError(f"Atlassian MCP tool error ({tool_name}): {resp['error']}")
        for item in resp.get("result", {}).get("content", []):
            if item.get("type") == "text":
                return item["text"]
        return ""

    def start(self):
        self.proc = subprocess.Popen(
            ["npx", "--yes", "mcp-remote", ATLASSIAN_MCP_URL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        init_resp = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "procore-voice-weekly-update", "version": "1.0"},
        })
        if "error" in init_resp:
            raise RuntimeError(f"Atlassian MCP init error: {init_resp['error']}")

        # Notification — no response expected
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

        # Resolve cloud ID (needed for all subsequent calls)
        text = self.call("getAccessibleAtlassianResources", {})
        try:
            resources = json.loads(text)
            if isinstance(resources, list) and resources:
                self.cloud_id = resources[0]["id"]
            else:
                raise RuntimeError(f"Unexpected resources response: {text[:200]}")
        except (json.JSONDecodeError, KeyError) as e:
            raise RuntimeError(f"Could not parse Atlassian cloud ID: {e}\nResponse: {text[:200]}")

        return self

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.stop()


def fetch_jira_data(atlassian: AtlassianMCP, config: dict, since: datetime) -> list:
    """Fetch closed/transitioned Jira tickets from FSAD + MARCH boards."""
    projects = config.get("jira_projects", ["FSAD", "MARCH"])
    base_url = config.get("atlassian_base_url", "https://procoretech.atlassian.net")
    since_str = since.strftime("%Y-%m-%d")
    project_filter = ", ".join(f'"{p}"' for p in projects)
    jql = (
        f'project in ({project_filter}) '
        f'AND updated >= "{since_str}" '
        f'AND status in ("Done", "Closed", "Released", "Resolved") '
        f'ORDER BY updated DESC'
    )

    text = atlassian.call("searchJiraIssuesUsingJql", {
        "cloudId": atlassian.cloud_id,
        "jql": jql,
        "maxResults": 50,
        "fields": "summary,status,assignee,priority,labels",
        "responseContentFormat": "markdown",
    })

    tickets = []
    try:
        data = json.loads(text)
        # Handle both {issues: [...]} and bare list
        issues = data.get("issues", data) if isinstance(data, dict) else data
        if isinstance(issues, list):
            for issue in issues:
                key = issue.get("key", "")
                fields = issue.get("fields", issue)
                status = fields.get("status", "")
                assignee = fields.get("assignee") or {}
                priority = fields.get("priority", "")
                tickets.append({
                    "key": key,
                    "summary": fields.get("summary", ""),
                    "status": status.get("name", "") if isinstance(status, dict) else str(status),
                    "assignee": assignee.get("displayName", "Unassigned") if isinstance(assignee, dict) else "Unassigned",
                    "priority": priority.get("name", "") if isinstance(priority, dict) else str(priority),
                    "labels": fields.get("labels", []),
                    "url": f"{base_url}/browse/{key}",
                })
        else:
            # Non-JSON markdown summary — pass through as-is for Claude
            tickets = [{"raw_summary": text}]
    except json.JSONDecodeError:
        tickets = [{"raw_summary": text}]

    print(f"  Jira: {len(tickets)} closed/transitioned tickets")
    return tickets


def fetch_confluence_context(atlassian: AtlassianMCP, config: dict) -> dict:
    """GET the current Confluence page body for context and prepend target."""
    page_id = config["confluence_page_id"]
    text = atlassian.call("getConfluencePage", {
        "cloudId": atlassian.cloud_id,
        "pageId": page_id,
        "contentFormat": "markdown",
    })

    try:
        data = json.loads(text)
        title = data.get("title", "Field AI - Weekly Updates")
        # Body may be nested under data.body or at the top level
        body = data.get("body", "") or text
        if isinstance(body, dict):
            body = body.get("value", "") or body.get("storage", {}).get("value", "") or str(body)
    except json.JSONDecodeError:
        title = "Field AI - Weekly Updates"
        body = text

    return {"title": title, "body": body}


def update_confluence_page(atlassian: AtlassianMCP, config: dict, new_section: str, current: dict) -> None:
    """Prepend new_section to the Confluence page and write back via MCP."""
    page_id = config["confluence_page_id"]
    date_header = datetime.now().strftime("%B %-d, %Y")
    new_body = f"## Week of {date_header}\n\n{new_section}\n\n---\n\n{current['body']}"

    atlassian.call("updateConfluencePage", {
        "cloudId": atlassian.cloud_id,
        "pageId": page_id,
        "title": current["title"],
        "body": new_body,
        "contentFormat": "markdown",
        "versionMessage": f"Weekly update {datetime.now().strftime('%Y-%m-%d')}",
    })
    print("  Confluence: page updated")


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
JOIN PROCORE_IT.GONG_DATA_CLOUD_PREP.GONG_CONVERSATION_PARTICIPANTS_PREP p
  ON c.CONVERSATION_KEY = p.CONVERSATION_KEY
  AND p.USER_ID IN (
    SELECT DISTINCT USER_ID
    FROM PROCORE_IT.GONG_DATA_CLOUD_PREP.GONG_CONVERSATION_PARTICIPANTS_PREP
    WHERE LOWER(EMAIL_ADDRESS) IN ('kyle.weatherholtz@procore.com', 'michael.sinai@procore.com')
      AND USER_ID IS NOT NULL
  )
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
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
    proc.stdin.write(payload)
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP process closed stdout")
    return json.loads(line)


def fetch_granola_notes(since: datetime) -> list:
    """Spawn npx mcp-remote, list meetings from the past week via MCP JSON-RPC."""
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
        init_resp = _mcp_rpc(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "procore-voice-weekly-update", "version": "1.0"},
        }, 1)
        if "error" in init_resp:
            print(f"  Granola MCP init error: {init_resp['error']}", file=sys.stderr)
            return []

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        # Step 1: list meetings in the time window
        list_resp = _mcp_rpc(proc, "tools/call", {
            "name": "list_meetings",
            "arguments": {
                "time_range": "custom",
                "custom_start": since.strftime("%Y-%m-%d"),
                "custom_end": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            },
        }, 2)

        meeting_ids = []
        for item in list_resp.get("result", {}).get("content", []):
            text = item.get("text", "")
            # Response is XML-like with email addresses in <> — use regex not XML parser
            for m in re.finditer(r'<meeting id="([^"]+)" title="([^"]+)" date="([^"]+)"', text):
                meeting_ids.append({"id": m.group(1), "title": m.group(2), "date": m.group(3)})

        if not meeting_ids:
            print("  Granola: 0 meetings found in time range")
            return []

        # Step 2: fetch full content in batches of 10
        req_id = 3
        for i in range(0, len(meeting_ids), 10):
            batch_meta = meeting_ids[i:i + 10]
            batch_ids = [m["id"] for m in batch_meta]
            detail_resp = _mcp_rpc(proc, "tools/call", {
                "name": "get_meetings",
                "arguments": {"meeting_ids": batch_ids},
            }, req_id)
            req_id += 1

            for item in detail_resp.get("result", {}).get("content", []):
                text = item.get("text", "")
                # Parse each <meeting ...> block with regex (email addresses break XML parsers)
                for match in re.finditer(
                    r'<meeting id="([^"]+)" title="([^"]+)" date="([^"]+)">(.*?)</meeting>',
                    text,
                    re.DOTALL,
                ):
                    mid, title, date, body = match.group(1), match.group(2), match.group(3), match.group(4)
                    summary_match = re.search(r'<summary>(.*?)</summary>', body, re.DOTALL)
                    summary = summary_match.group(1).strip()[:1000] if summary_match else ""
                    notes.append({"title": title, "date": date, "content": summary})

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


def _anthropic_client(config: dict) -> anthropic.Anthropic:
    """Build an Anthropic client, falling back to the Claude Code gateway if no API key is set."""
    api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    base_url = None

    if not api_key:
        # Reuse the Claude Code LLM gateway from ~/.claude/settings.json
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.exists():
            with open(settings_path) as f:
                settings = json.load(f)
            env = settings.get("env", {})
            api_key = env.get("ANTHROPIC_AUTH_TOKEN")
            base_url = env.get("ANTHROPIC_BASE_URL")
            # Stash gateway model IDs for use in synthesize_update
            config.setdefault("_gateway_opus_model", env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"))
            config.setdefault("_gateway_sonnet_model", env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"))

    if not api_key:
        sys.exit("No Anthropic API key found. Set anthropic_api_key in config.json or ANTHROPIC_API_KEY in environment.")

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def synthesize_update(all_data: dict, config: dict, today: datetime) -> str:
    """Call Claude API to synthesize the LT update from all collected data."""
    client = _anthropic_client(config)
    model = (
        config.get("anthropic_model")
        or config.get("_gateway_opus_model")
        or "claude-opus-4-6"
    )

    date_str = today.strftime("%B %-d")

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
        model=model,
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
# Output archive
# ---------------------------------------------------------------------------

def save_output(text: str, today: datetime) -> Path:
    output_dir = Path.home() / "AI-workshop" / "Procore Voice" / "updates"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"LT Update - {today.strftime('%Y-%m-%d')}.md"
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

    with AtlassianMCP() as atlassian:
        print(f"  Atlassian MCP: connected (cloud_id={atlassian.cloud_id})")
        jira_data = fetch_jira_data(atlassian, config, since)
        confluence_ctx = fetch_confluence_context(atlassian, config)
        print(f"  Confluence: loaded page '{confluence_ctx['title']}'")

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
            update_confluence_page(atlassian, config, update_text, confluence_ctx)
            print()
            print("Done.")


if __name__ == "__main__":
    main()
