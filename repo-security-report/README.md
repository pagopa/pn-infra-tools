# dependabot-report

Generates a GitHub security report for a list of repositories, covering:

- **Dependabot** vulnerability alerts (grouped by severity)
- **Code scanning** alerts — CodeQL and other tools (grouped by severity)
- **Secret scanning** alerts (active count)
- **Renovate PRs** — open pull requests from the Renovate bot

The report can be published to **Confluence** (space `PN`) as a static page that lists **every** repository, with a detailed CSV of all issues attached. The Confluence page is not filterable.

## Requirements

- [`gh` CLI](https://cli.github.com/) ≥ 2.0
- [`jq`](https://stedolan.github.io/jq/)
- Python 3.9+

## Authentication

The script calls the GitHub REST API directly using `GH_TOKEN`; it does not read the `gh` CLI credential store. After completing the CLI login steps below, run `export GH_TOKEN="$(gh auth token)"` before invoking the report, or set `GH_TOKEN` in `.env` as shown below. A **401 Bad credentials** response indicates that the token is invalid or expired.

### Step 1 — Log in

```bash
gh auth login
```

Follow the interactive prompts (browser or token). Choose **GitHub.com** and grant at least the scopes below.

### Step 2 — Verify required scopes

| Scope | Why |
|---|---|
| `repo` | Read access to private repositories and their security alerts |
| `security_events` | Read Dependabot, code-scanning, and secret-scanning alerts |

Check your current scopes:

```bash
gh auth status
```

If scopes are missing, refresh your token:

```bash
gh auth refresh -h github.com -s repo,security_events
```

### Step 3 — Re-run the script

```bash
./dependabot-report.sh pagopa/pn-infra pagopa/pn-delivery
```

### Using a Personal Access Token (PAT) instead

```bash
# In .env: GH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
./dependabot-report.sh pagopa/pn-infra
```

> **Fine-grained PATs**: grant **Read-only** access to *Dependabot alerts*, *Code scanning alerts*, *Secret scanning alerts*, and *Pull requests* (under Repository permissions) for every target repository.  
> **Classic PATs**: enable the `repo` and `security_events` scopes.

### Common causes of 401

| Symptom | Fix |
|---|---|
| Never logged in | `gh auth login` |
| Token expired / revoked | `gh auth login` again or regenerate the PAT |
| Missing `security_events` scope | `gh auth refresh -s repo,security_events` |
| Organisation SSO not authorised | Go to github.com → Settings → PAT → *Authorise* for the org |
| Fine-grained PAT missing repo access | Edit the PAT and add the target repos |

---

## Usage

```text
./dependabot-report.sh [OPTIONS] [owner/repo ...]

Options:
  -f, --file FILE           Read repository list from FILE (one owner/repo per line)
  -o, --output FORMAT       Output format: table (default), csv, json
  -s, --state STATE         Alert state: open (default), dismissed, fixed, auto_dismissed
  --renovate-user PREFIX    GitHub login prefix for the Renovate bot (default: renovate-pagopa)
  --issues-csv FILE         Write a detailed CSV of every security issue
  --input-csv FILE          Reuse a summary or detailed CSV without querying GitHub
  --out-file FILE           Write the summary report to FILE (relative paths go in ./output/) instead of stdout
  --confluence              Publish to Confluence and attach the detailed CSV
  --confluence-url URL      Confluence API URL (default: api.atlassian.com/ex/confluence/<cloudId>)
  --confluence-cloud-id ID  Confluence Cloud ID (default: f985e071-a2d6-442d-a5f4-a425cf8beb72)
  --confluence-space KEY    Space key (default: PN)
  --confluence-user EMAIL   Atlassian account email
  --confluence-token TOKEN  Confluence API token
  --confluence-parent-id ID Optional parent page id
  --env-file FILE           Load env vars from FILE (default: ./.env or script-dir/.env)
  -h, --help                Show help
```

### Examples

```bash
# Pass repos directly
./dependabot-report.sh pagopa/pn-infra pagopa/pn-delivery

# Read from a file (see repos.txt.example)
./dependabot-report.sh -f repos.txt

# CSV output
./dependabot-report.sh -f repos.txt -o csv > report.csv

# JSON output (used as input for the HTML report)
./dependabot-report.sh -f repos.txt -o json > report.json

# Custom Renovate bot username prefix
./dependabot-report.sh -f repos.txt --renovate-user my-renovate-bot

# Include dismissed alerts
./dependabot-report.sh -f repos.txt -s dismissed

# Publish to Confluence (space PN) and attach the detailed issues CSV
# Credentials are read from .env (see env.example)
./dependabot-report.sh -f repos.txt --confluence
```

---

## CSV-first workflow (no repository reprocessing)

```bash
# 1. Query GitHub once and save the detailed issues snapshot
./dependabot-report.sh -f repos.txt --issues-csv report.csv

# 2. Publish the saved snapshot using the existing Confluence template
./dependabot-report.sh --input-csv report.csv --confluence

# 3. Generate the interactive HTML using the existing template
python3 generate-html-report.py -i report.csv -o report.html
```

Steps 2 and 3 do not query GitHub or require `GH_TOKEN`. Confluence credentials
are required only for step 2. Publishing attaches the original input CSV and
uses the snapshot's date for the page title, so repeating it updates the same page.

Both CSV formats are accepted automatically:

- **Detailed issues CSV** (`--issues-csv`): aggregates individual findings and
  attaches their full details to Confluence.
- **Repository summary CSV** (`-o csv --out-file report.csv`): uses the existing
  counts, including zero-finding repositories, and ignores the `TOTAL` row when
  aggregating. Confluence labels the attachment as a summary, not issue details.

Relative output paths (`--out-file`, `--issues-csv`, and the HTML `-o`) are written inside the git-ignored `output/` folder; relative inputs not found as given are looked up there too.

File-based CSV generation also writes **`report.csv.metadata.json`** alongside
the CSV. Keep both files together. The sidecar preserves the generation timestamp,
report date, alert state, Renovate bot prefix, and complete repository list,
including repositories without findings. A checksum rejects a CSV that has been
modified without updating its metadata. Use `--out-file` rather than stdout
redirection to create metadata for a summary CSV.

Existing CSVs without a sidecar still work, with a warning: the alert state is
shown as `unknown`, the file modification time supplies the timestamp, and the
date prefix in the filename (if present) supplies the page date. Otherwise, the
file modification date is used. Detailed CSVs without metadata cannot recover
zero-finding repositories. Individual issues cannot be reconstructed from a
summary CSV.

`--input-csv` cannot be combined with repository arguments, `--file`, or
`--issues-csv`. The snapshot supplies the alert state and bot prefix; `--state`
and `--renovate-user` apply only when querying GitHub.

---

## Confluence

`--confluence` creates or updates a page named **`YYYY-MM-DD Report di sicurezza Github`** in space **PN**. The page content is in Italian.

The page includes:

- A **Scarica CSV** button for the attached issues file
- Totals by source and severity
- **Every component** (repository) in the input list, including those with zero findings
- An **apri** link after each non-zero count (Dependabot, code scanning, secrets, Renovate)
- Per-source breakdown tables (the full issue list is only in the CSV attachment)

It also attaches **`YYYY-MM-DD-github-security-issues.csv`**, with one row per Dependabot alert, code-scanning alert, secret-scanning alert, and Renovate PR.

### Authentication via `.env`

Copy `env.example` to `.env` in this folder (or in the working directory) and fill in the values. The script loads it automatically. Variables already set in the shell are not overwritten.

```bash
cp env.example .env
```

Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

| Variable | Required | Description |
|---|---|---|
| `GH_TOKEN` | when querying GitHub | GitHub token with `repo` and `security_events`; not needed with `--input-csv` |
| `CONFLUENCE_API_TOKEN` (or `CONFLUENCE_API_KEY`) | for `--confluence` | Atlassian API token |
| `CONFLUENCE_USER` (or `CONFLUENCE_EMAIL`) | recommended | Atlassian account email (Basic auth) |
| `CONFLUENCE_CLOUD_ID` | no | Default `f985e071-a2d6-442d-a5f4-a425cf8beb72` |
| `CONFLUENCE_URL` | no | Default `https://api.atlassian.com/ex/confluence/<cloudId>` |
| `CONFLUENCE_SITE_URL` | no | Browser links; default `https://pagopa.atlassian.net` |
| `CONFLUENCE_SPACE` | no | Default `PN` |
| `CONFLUENCE_PARENT_ID` | no | Create the page under this parent |

Use `--env-file PATH` to load a file that is not named `.env`. If `CONFLUENCE_USER` is omitted, the token is sent as a Bearer token.

Re-running the command on the same day updates the existing page and replaces the CSV attachment.

### Input file format

```
# Lines starting with # are ignored
pagopa/pn-infra
pagopa/pn-delivery
pagopa/pn-delivery-push
```

---

## HTML report

Generate an interactive single-file HTML report from either CSV format as shown
above, or from the existing JSON output:

```bash
# 1. Produce JSON
./dependabot-report.sh -f repos.txt -o json --out-file report.json

# 2. Generate HTML
python3 generate-html-report.py -i report.json -o report.html

# 3. Open in browser
open report.html
```

The HTML report includes:
- **Source tabs** — switch between All Security, Dependabot, Code Scanning, Secrets, Renovate PRs
- **Stat cards** — totals per severity and per source
- **Bar chart** — top repositories by alert count
- **Donut chart** — severity distribution (available for severity-based sources)
- **Interactive table** — sortable columns, severity filter chips, repository search and multi-select

---

## JSON schema

```json
{
  "generated_at": "2026-06-23T08:00:00Z",
  "alert_state": "open",
  "renovate_user": "renovate-pagopa",
  "repositories": [
    {
      "repository": "pagopa/pn-infra",
      "dependabot":     { "critical": 2, "high": 5, "medium": 3, "low": 1, "unknown": 0, "total": 11 },
      "code_scanning":  { "critical": 0, "high": 2, "medium": 4, "low": 0, "unknown": 1, "total": 7  },
      "secret_scanning": 3,
      "renovate_prs": 7,
      "total": 28
    }
  ],
  "totals": {
    "dependabot":     { "critical": 2, "high": 5, "medium": 3, "low": 1, "unknown": 0, "total": 11 },
    "code_scanning":  { "critical": 0, "high": 2, "medium": 4, "low": 0, "unknown": 1, "total": 7  },
    "secret_scanning": 3,
    "renovate_prs": 7,
    "total": 28
  }
}
```

---

## Sample terminal output

```
  GitHub Security Report
  Alert state : open
  Generated   : 2026-06-23 08:00 UTC

  Dependabot Alerts
+-----------------------------------------------+-----------+---------+---------+---------+-----------+-----------+
| REPOSITORY                                    | CRITICAL  | HIGH    | MEDIUM  | LOW     | UNKNOWN   | TOTAL     |
+-----------------------------------------------+-----------+---------+---------+---------+-----------+-----------+
| pagopa/pn-infra                               | 2         | 5       | 3       | 1       | 0         | 11        |
| pagopa/pn-delivery                            | 0         | 3       | 7       | 2       | 0         | 12        |
+-----------------------------------------------+-----------+---------+---------+---------+-----------+-----------+
| TOTAL                                         | 2         | 8       | 10      | 3       | 0         | 23        |
+-----------------------------------------------+-----------+---------+---------+---------+-----------+-----------+

  Code Scanning Alerts (CodeQL)
+-----------------------------------------------+-----------+---------+---------+---------+-----------+-----------+
| REPOSITORY                                    | CRITICAL  | HIGH    | MEDIUM  | LOW     | UNKNOWN   | TOTAL     |
...

  Secret Scanning & Renovate PRs
+-----------------------------------------------+----------------+-------------------+
| REPOSITORY                                    | ACTIVE SECRETS | RENOVATE PRs OPEN |
+-----------------------------------------------+----------------+-------------------+
| pagopa/pn-infra                               | 3              | 7                 |
| pagopa/pn-delivery                            | 0              | 2                 |
+-----------------------------------------------+----------------+-------------------+
| TOTAL                                         | 3              | 9                 |
+-----------------------------------------------+----------------+-------------------+
```
