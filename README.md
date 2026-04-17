# Procore Voice Weekly Update

Automated Friday digest that collects data from GitHub, Jira, Confluence, Gong, and Granola — synthesizes it into an LT update using Claude — then prepends it to the standing Confluence page.

**Target page:** [Field AI - Weekly Updates](https://procoretech.atlassian.net/wiki/spaces/.../pages/5282955265/Field+AI+-+Weekly+Updates)
**Schedule:** Every Friday at 4:00 PM (via launchd)

---

## Setup

### 1. Install dependencies

```bash
pip3 install anthropic requests
```

`snow` CLI (Snowflake) should already be installed and have a cached Okta session from any recent `gong-daily-digest` run.

### 2. Generate tokens

- **Atlassian API token:** https://id.atlassian.net/manage-profile/security/api-tokens
- **Anthropic API key:** https://console.anthropic.com/settings/keys

### 3. Create config.json

```bash
cp config.example.json config.json
# Edit config.json — fill in atlassian_api_token and anthropic_api_key
```

`config.json` is gitignored. **Never commit it.**

The GitHub PAT is read automatically from `~/.cursor/mcp.json` — no manual entry needed.

### 4. Register the launchd job

```bash
bash setup.sh
```

This installs dependencies, validates config, and registers the Friday 4pm launchd job.

---

## Testing (recommended before first live run)

```bash
# 1. Full dry run on last week's data
python3 weekly_update.py --dry-run --since 2026-04-07

# 2. Confirm this week's lookback window
python3 weekly_update.py --dry-run

# 3. Skip sources if their auth has lapsed
python3 weekly_update.py --dry-run --skip-gong
python3 weekly_update.py --dry-run --skip-granola
```

Each run (including dry runs) saves the generated update to `output/YYYY-MM-DD.md` for review.

---

## CLI Flags

| Flag | Description |
|---|---|
| `--dry-run` | Print synthesized update to stdout; skip Confluence write |
| `--since YYYY-MM-DD` | Override 7-day lookback window |
| `--skip-gong` | Skip Snowflake/Gong (use if Okta session is expired) |
| `--skip-granola` | Skip Granola MCP (use if auth has lapsed) |

---

## File Structure

```
procore-voice-weekly-update/
├── weekly_update.py                          # Main script
├── config.example.json                       # Template (committed)
├── config.json                               # Live credentials (gitignored)
├── setup.sh                                  # One-time install + launchd registration
├── com.procore.voice-weekly-update.plist     # launchd job definition
├── README.md                                 # This file
└── output/                                   # Archived runs (gitignored)
    └── YYYY-MM-DD.md
```

---

## Data Sources

| Source | What is collected | Auth |
|---|---|---|
| GitHub | Merged PRs + commits on `procore/voice-ios` | PAT from `~/.cursor/mcp.json` |
| Jira | Tickets closed/transitioned (FSAD + MARCH boards) | Atlassian API token (Basic auth) |
| Confluence | Current page body (for milestone continuity) | Same Atlassian token |
| Gong (Snowflake) | Call summaries mentioning voice/field AI | `snow` CLI + cached Okta session |
| Granola | Meeting notes from the past week | `npx mcp-remote` + Granola auth |

---

## Logs

```
~/Library/Logs/procore-voice-update.log        # stdout
~/Library/Logs/procore-voice-update-error.log  # stderr
```

---

## Troubleshooting

**Gong returns no data / auth error**
Okta session has expired. Run any `gong-daily-digest` command to refresh it, then retry with `--skip-granola` if needed.

**Granola returns no notes**
Re-authenticate via the Granola app or browser. Use `--skip-granola` to proceed without it.

**Confluence PUT fails with 409 Conflict**
Another process updated the page between your GET and PUT. Re-run; the script will fetch the latest version.

**launchd job not firing**
Check `launchctl list | grep procore` and review error logs. Re-run `bash setup.sh` to reload.
