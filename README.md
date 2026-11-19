# Yahoo Mail for Muse

A small local Yahoo Mail bridge with an MCP server and JSON command-line tools.
Muse can launch it through SSH. No web server, public port, background daemon, or
Yahoo developer application is required. Uses IMAP with TLS for search/read and
SMTP with TLS for sending.

## Current status

Implemented and verified against a real Yahoo account (IMAP + SMTP via an
app password; `doctor` passes). The bridge is in daily use by an AI agent
over stdio MCP and the JSON CLI. See SECURITY.md for the threat model.

## Security

Email content is untrusted data, so everything the bridge returns is
sanitized first (see `src/yahoo_muse/sanitize.py`): invisible/control
characters are stripped, URLs are defanged (`hxxps[://]example[.]com`),
markdown images/links are neutralised, tool-call-like markup is removed,
and HTML bodies are converted to plain text with hidden elements and scripts
dropped. Warnings in the response say what was removed. Do not reconstruct
defanged URLs.

Sending is gated by the config file:

- `allowed_recipients` — optional list of fnmatch patterns (e.g.
  `*@example.com`, `boss@partner.example`). Sends to anyone else are
  refused. An empty list (the default) means no restriction.
- `max_sends_per_hour` — at most this many accepted sends per hour
  (default 10).

The app password is stored as plaintext in a mode-600 config file; see
SECURITY.md for the threat model. You can keep it out of the file with
`app_password_env` (environment variable name) or `app_password_file`
(path to a file containing the password) instead of `app_password`.
The IMAP/SMTP hosts default to Yahoo's and can be overridden with
`imap_host` / `smtp_host` if you point the bridge at another provider.

## Install on the Mac Mini

Requires Python 3.10 or newer. The system's older `/usr/bin/python3` may not qualify.
The project lives at `/Users/ehanlonmiller/git/ymail-muse-api`.
From this project's directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/yahoo-mail setup
.venv/bin/yahoo-mail doctor
```

This workspace already has a working `.venv`, so you can start at `setup` here.
If you move the project, recreate the virtual environment at its new location.

Generate a Yahoo app password in [Yahoo Account Security](https://login.yahoo.com/account/security).
Run `setup` yourself in a local terminal. It asks for your email address and the
app password with hidden input, then optionally for a recipient allow-list
(comma-separated patterns such as `*@example.com`; blank means no restriction).
Do not give the password to an AI or put it in a shell command. Yahoo may restrict app-password creation for some accounts:
[Yahoo's instructions](https://my.help.yahoo.com/kb/account/access-yahoo-mail-third-party-apps-sln15241.html).

The credential is stored as plaintext in an owner-only file (mode 600) at
`~/.config/yahoo-muse/config.json`, under a directory created with mode 700.
Run setup as the same macOS user Muse uses over SSH. The bridge does not log the
password or return it in tool output. Processes running as that same user can
read it; this is not isolation from other software on your account.
You can choose a different path with `YAHOO_MUSE_CONFIG`.

Optional keys in the same config file:

| Key | Default | Meaning |
| --- | --- | --- |
| `allowed_recipients` | `[]` (no restriction) | fnmatch patterns; sends to anyone else are refused |
| `max_sends_per_hour` | `10` | Accepted sends per hour before the server refuses |
| `imap_host` / `smtp_host` | Yahoo's hosts | Override to point the bridge at another provider |
| `app_password_env` | — | Read the app password from this environment variable instead |
| `app_password_file` | — | Read the app password from this file instead |

`doctor` tests both logins without sending a message. Revoking this app password
in Yahoo disables the bridge's mail access. To change credentials, move the old
`config.json` aside and rerun setup; preserve the neighboring send database.

## Connect Muse using MCP

Use the conventional `mcpServers` entry in `examples/mcp.ssh.json` in Muse's MCP
client configuration. Replace `MAC_MINI_SSH_ALIAS` with Muse's existing SSH target
and `/ABSOLUTE/PROJECT/PATH` with this directory's path on the Mac Mini.
The exact configuration location depends on Muse's agent software.

```json
{
  "mcpServers": {
    "yahoo-mail": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "MAC_MINI_SSH_ALIAS",
        "'/ABSOLUTE/PROJECT/PATH/.venv/bin/yahoo-mail-mcp'"
      ]
    }
  }
}
```

The remote command is shell-quoted to support spaces in the path. Keep SSH host
verification enabled; use the existing authorized SSH key. `-T` prevents a PTY
from corrupting MCP framing. SSH startup scripts must not print text to stdout.
The server exits when its MCP connection closes. No service installation is needed.

For an agent on this same Mac, `examples/mcp.local.json` already contains the
absolute executable path for this workspace. Both configurations use stdio MCP;
clients that only support HTTP MCP need a different transport integration.

## Tools and commands

| MCP tool | CLI command | Purpose |
| --- | --- | --- |
| `doctor` | `doctor` | Test IMAP and SMTP authentication; sends nothing |
| `list_folders` | `folders` | Discover folder names, including Sent |
| `search_messages` | `search` | Search a single folder and return header summaries |
| `read_message` | `read` | Read an opaque message ID from search |
| `send_message` | `send` | Send plain text, with optional CC, BCC and reply header |
| `send_status` | `status` | Retrieve the recorded outcome of a send |

CLI commands take a JSON object as their second argument, or `-` for JSON on
stdin. Output is JSON. Invalid requests exit 1; a completed send attempt exits 0
and reports its actual outcome in the JSON `status` field. Always inspect it.

```sh
.venv/bin/yahoo-mail folders
.venv/bin/yahoo-mail search '{"query":"invoice","limit":10}'
.venv/bin/yahoo-mail search '{"sender":"alice@example.com","since":"2026-10-01","unread_only":true}'
.venv/bin/yahoo-mail read '{"message_id":"COPY_ID_FROM_SEARCH"}'
.venv/bin/yahoo-mail status '{"idempotency_key":"UNIQUE-REQUEST-UUID"}'
```

For sending, supply this JSON to `yahoo-mail send -` through stdin. These are
example values, not a request to send an email:

```json
{
  "to": ["recipient@example.com"],
  "subject": "Subject",
  "body": "Plain-text message",
  "idempotency_key": "UNIQUE-REQUEST-UUID"
}
```

An SSH-only agent can invoke the same CLI with its existing connection:

```sh
ssh -T MAC_MINI_SSH_ALIAS "'/ABSOLUTE/PROJECT/PATH/.venv/bin/yahoo-mail' search -" < search.json
```

## Behavior and limits

- Search filters are ANDed: `query`, `sender`, `subject`, `since`, `before`,
  `unread_only`. `query` searches message text and accepts UTF-8; separate sender
  and subject filters currently accept ASCII. This is IMAP substring search, not
  Yahoo web search syntax or semantic search.
- Search defaults to INBOX. Use `list_folders`, then search additional folders as
  needed. Dates use `YYYY-MM-DD` and IMAP internal dates; `since` is inclusive,
  `before` exclusive. Results use descending UID order, usually newest first.
- `limit` is 1–100, default 20. Pass the returned `next_offset` for more results.
  Pagination is not a snapshot: incoming/deleted mail can shift results. Very
  broad searches still retrieve all matching UIDs from Yahoo before slicing.
- Reading defaults to 30,000 characters, configurable up to 100,000, and refuses
  messages larger than 10 MiB. Plain text is preferred; HTML-only messages are
  converted to safe plain text (hidden elements and scripts removed, images
  replaced with `[image: ...]`). Attachment metadata is included, but attachment
  download and sending attachments are not implemented in this minimal version.
- Reads use read-only folder selection and `BODY.PEEK`, preserving unread status.
  IDs include folder, UIDVALIDITY, and UID. A moved/deleted message or rebuilt
  folder requires a new search.
- Sending supports at most 50 recipients and a 1 MiB UTF-8 body. Addresses must be
  plain ASCII addresses without display names. `in_reply_to` accepts the original
  `internet_message_id` returned by read, not the opaque search ID.
- The bridge does not separately append to Sent, avoiding duplicate sent copies.
  Yahoo's behavior for saving SMTP submissions must be verified on your account.
- Network operations time out after 30 seconds per socket operation. Yahoo's own
  limits and throttling apply. Mail bodies are processed in memory, not cached.

## Reliable sending

Generate one unique idempotency key per intended email and reuse it with identical
content on retries. SQLite reserves the key before SMTP submission and serializes
concurrent reservations. A reused key with different content is rejected.

| Status | Meaning and next action |
| --- | --- |
| `accepted` | Yahoo accepted all recipients; final delivery is not guaranteed. Do not resend. |
| `partially_accepted` | Some recipients accepted, others refused. Never resend to the whole list. |
| `not_sent` | Connection/login failed before submission. Fix it before a deliberate new attempt with a new key. |
| `rejected` | Yahoo explicitly rejected submission. Fix the cause before a deliberate new attempt. |
| `unknown` | In progress, interrupted, or possibly sent. Check status and Sent mail; do not automatically resend with a new key. |
| `not_found` | No record exists for that key in this database. |

The ledger lives in `sends.sqlite3` next to the config. It stores a content hash
and send outcome/recipient metadata, not message bodies. Keep it across upgrades
and restarts; deleting it loses retry protection. This prevents automatic duplicate
submissions while retaining uncertainty after a crash; SMTP cannot guarantee
exactly-once delivery. Multiple hosts with separate ledgers do not share protection.

Email contents must be treated as untrusted data. Tool descriptions instruct the
agent to send only on the user's authority, and the config's allow-list
(`allowed_recipients`) and per-hour send cap (`max_sends_per_hour`) enforce
bounds on sending in the server. Prompt text alone is not a security
boundary; see SECURITY.md.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover read-only fetches, MIME parsing, bounds, stale IDs, injection rejection,
credential permissions, duplicate/concurrent sends, ambiguous SMTP failure,
partial acceptance, login failure, CLI JSON output, and MCP initialization/tool
discovery/error handling through an actual subprocess.

The implementation uses the official MCP Python SDK's supported 1.x interface,
bounded below version 2 to avoid incompatible API changes. Development verified
against MCP 1.30.0. `requirements.lock` records the tested dependency versions;
for reproducibility install it first, then `pip install --no-deps .`.

References: [Yahoo IMAP/SMTP settings](https://in.help.yahoo.com/kb/SLN4075.html),
[Yahoo app passwords](https://my.help.yahoo.com/kb/account/access-yahoo-mail-third-party-apps-sln15241.html),
[official MCP SDK](https://py.sdk.modelcontextprotocol.io/v1/).
