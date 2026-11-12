# outlook-mailer

A small self-hosted web app for sending a holiday email campaign from a free personal @outlook.com account. Upload a
CSV, map the email and first-name columns, write the message in Markdown, preview it in a Christmas-styled HTML
template, send yourself a test, then run a rate-limited bulk send through Microsoft Graph (one email per recipient,
with a CAN-SPAM footer and a one-click unsubscribe link). It runs as a single Docker container.

> **Status: under construction.** So far: configuration, `/healthz`, the Docker image, the admin UI shell with
> the Cloudflare Access check and the help page, CSV upload with column mapping and the validation report,
> merge fields, the Markdown-to-email conversion of the message body, the Christmas email template with its
> footer and hosted images, the compose page with saved footer settings and a light/dark preview, the
> MIME message builder with the mock transport, and the send engine with its progress page,
> pause/resume/cancel and results download, and the Microsoft sign-in (device code) with its Account page.
> The Graph transport and the test-send gate come next.
> See [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Overview

_To be written._

## How it works

### Access

The app is reached through a Cloudflare tunnel and protected by a Cloudflare Access application, which decides who
can sign in. Everything is protected except `/u/*` (unsubscribe links), `/img/*` (images in emails) and `/healthz`;
those need a Bypass policy in Access because email recipients and health checks must reach them without signing in.
The app also checks the Access JWT itself (the `Cf-Access-Jwt-Assertion` header: RS256, Cloudflare's published keys,
audience and issuer) on every other request, so the admin pages can't be used by going to the container's port
directly. Forms carry a CSRF token and POSTs must come from `PUBLIC_BASE_URL`. Full Cloudflare setup comes later in
`docs/cloudflare.md`.

### CSV and column mapping

On **New send** you upload any CSV whose first row holds the column names (a Square customer export works as-is).
The app works out the encoding (UTF-8, UTF-8 with BOM, or Windows/Excel cp1252) and the separator (comma, semicolon
or tab), then shows three dropdowns filled from the header: **Email address** and **First name** (both required)
and **Email subscription status** (optional). They are preselected from the column names, e.g. `Email Address`,
`First Name` and `Email Subscription Status`, and can be changed. If you pick a status column, the app lists its
distinct values with counts and you tick the ones to send to; values that look like opt-outs (`unsubscribed`,
`bounced`, `not_subscribed`, `no`, `false`, `opted out`, `pending`, `unknown`…) start unticked, while `subscribed`
or `opted in` start ticked. A blank status is listed as "(blank — no status)" and also starts unticked; tick it to
send to rows that have an email address but no status. Square exports use `subscribed`, `unsubscribed`, blank and
`bounced`. Leave status on *none* and everyone on the list is eligible. Other columns are ignored. The next page
reports how many rows will be sent to and why the rest won't: missing or invalid email, duplicate address (the first
row is kept), skipped by status, or already in `unsubscribed.txt`. A missing first name is only a warning (the
merge field fallback is used). The full list of problems can be downloaded as a CSV. The uploaded file is kept in
`DATA_DIR/uploads/` (readable only by the app) until the campaign ends. Limits: `MAX_UPLOAD_MB` and
`MAX_RECIPIENTS`.

### Message body and merge fields

The subject and body can use `{{first_name}}`, `{{email}}` and `{{first_name|Fallback}}`; an unknown or malformed
field blocks sending. The body is Markdown: `**bold**`, `*italic*`, `~~strike~~`, `++underline++`, `[text](url)`,
bullet and numbered lists, `#` and `##` headings, a blank line for a new paragraph and Enter for a new line. Raw HTML
is shown as text, and links must be `https`, `http` or `mailto`. A merge field may appear in a link only after a
fixed `https://host/` part or as exactly `mailto:{{email}}`, and values placed in links are percent-encoded. The body
becomes an HTML fragment with inline styles plus a plain-text version (`app/content.py`).

### Email template

Every email uses one Christmas template (`app/templates/email/christmas.html.j2`, based on
[Cerberus](https://github.com/TedGoas/Cerberus) by Ted Goas, MIT): a header band with a Christmas-tree photo behind
the business name and "Merry Christmas", a cream card holding your message, and a footer with the business name,
postal address, "This message is an advertisement." (can be switched off), why the person is getting the email, and
an **Unsubscribe** link. It is designed to work in Outlook (classic and new), Outlook.com, Gmail and Apple Mail,
light and dark mode, and reads fine with images turned off. The plain-text version ends with the same footer.
Images are served by the app at `/img/...` (public, no sign-in) and are public domain or the project's own work; see
`app/static/email-img/CREDITS.md`. `docs/sample-email.html` shows a rendered example and `docs/email-qa.md` is the
checklist for testing it in real mail apps.

### Compose and preview

After the list check, **Continue to compose** opens the compose page: the subject and an optional preview text
(both can use merge fields), the Markdown message, the header headline and tagline (default "Merry Christmas" /
"and a Happy New Year"), and the footer: business or sender name, postal address, the advertisement notice
(on by default) and an optional "why you're receiving this" sentence. **Save** checks everything and shows each
problem next to its field, keeping what you typed. The message is saved with the upload; the footer and header are
also remembered app-wide, so the next send starts with them filled in. Below the form, the preview shows the last
saved version exactly as it will be sent (same rendering, real unsubscribe link) for any of the first 50
recipients or for a sample with no first name (to check fallbacks), in light or dark colours, plus the subject
line and the plain-text version. Dark is an approximation of Apple Mail's dark mode; other mail apps change colours
their own way. The page also shows the email's size for the longest name and address on the list: it warns above
90 KB and refuses to save above 102 KB, where Gmail cuts messages off and would hide the unsubscribe link.
Unsubscribe links are `PUBLIC_BASE_URL/u/<token>`, where the token is the address (base64url) plus a truncated
HMAC-SHA256 made with `UNSUBSCRIBE_SECRET` (`app/unsubscribe_tokens.py`).

### Messages

Each recipient gets their own email (`app/message.py`): a standard MIME message with a plain-text part followed by
the HTML part, both UTF-8 (quoted-printable, or base64 for text that is mostly non-Latin), addressed to that one
person in `To` and nobody else (no `Cc`, `Bcc` or `Reply-To`). Headers: `From`, `To` (with a display name, safely
quoted or encoded), `Subject`, `Date`, `Message-ID`, `List-Unsubscribe: <PUBLIC_BASE_URL/u/<token>>` and
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`, so mail apps that support it (Gmail, Apple Mail, Outlook.com)
can show their own unsubscribe button. A header value containing a line break or control character is refused.
Microsoft Graph gets the message base64-encoded. With `MAIL_TRANSPORT=mock` nothing is sent: each message is written
to `DATA_DIR/outbox/<time>-<n>-<recipient>.eml` (readable only by the app), which opens in any mail app, and the
`From` is the placeholder `sender@example.invalid`.

### Microsoft sign-in

Real sending needs the Outlook.com account connected once, on the **Account** page (first set up the app
registration in [docs/entra-setup.md](docs/entra-setup.md) and set `MS_CLIENT_ID`). **Connect Outlook account**
shows a short code and the link Microsoft returns (normally `https://www.microsoft.com/link` for personal
accounts; the app shows whatever Microsoft sends, never a fixed address). Open the link on any device, enter the
code within 15 minutes, sign in with the @outlook.com account and select **Accept**. The page updates by itself
(or use **Refresh**) and then shows *Sending as you@outlook.com (Your Name)*; that address is the `From` of every
email. Only one sign-in runs at a time and it can be cancelled; reconnecting and then cancelling keeps the
existing sign-in. Work or school accounts are refused. The menu shows whether the account is connected (or
needs connecting again). After Microsoft refuses a token (HTTP 401) the app asks Microsoft for a fresh one on the next
send instead of reusing the saved one; *reconnect needed* goes away only when that renewal succeeds, a send is
accepted, or you connect again.

The app asks for `Mail.Send` and `User.Read` (MSAL adds `offline_access`, which lets it renew access by itself).
The saved sign-in (MSAL's token cache) is `DATA_DIR/msal_cache.json`, readable only by the app (mode 0600) and
replaced atomically. With `TOKEN_CACHE_KEY` set it is encrypted (Fernet); without it the file is plain JSON and the
Account page recommends setting a key. Setting a key later encrypts the existing file at the next start. If the key
changes or is removed, the saved sign-in can't be read: the page says so and the account has to be connected again
(nothing crashes; putting the old key back also works). Tokens and codes are never logged.

You only reconnect after changing the Outlook password, removing the app's access, or about 90 days without use;
errors from Microsoft are shown in plain words with the code (e.g. `AADSTS7000218` = public client flows not
turned on) and a link to the troubleshooting table in [docs/entra-setup.md](docs/entra-setup.md#troubleshooting).
**Sign out** deletes the saved sign-in and the stored address. To withdraw the app's access on Microsoft's side,
use [account.live.com/consent/Manage](https://account.live.com/consent/Manage). With `MAIL_TRANSPORT=mock` no
sign-in is needed and the app never contacts Microsoft.

### Sending

When the preview looks right, **Start sending** on the compose page opens a confirmation page: how many people
will be emailed, the pacing, the daily limit and an estimated time. Starting takes a snapshot of the saved message
and of the eligible recipients, so editing the draft afterwards doesn't change a running send. Only one send can be
unfinished at a time, and each upload is sent once.

### Test send

Sending to the list is **locked until a test of the current version has gone out to you**. On the compose page,
**Send test to me** sends the saved message once to the connected Outlook address (with `MAIL_TRANSPORT=mock`, to
the outbox, addressed to `you@example.com`), with `[TEST]` before the subject. Pick whose merge fields to use (one
of the first 50 recipients, or the sample with no first name); the unsubscribe link in a test is always for your
own address, so clicking it can't unsubscribe a customer. The compose and start pages then say *Test sent to you at
... for this version*. Any edit changes the version (a hash of the saved content), so the pages say *The message
changed since your last test* and the start button disappears until a new test goes out; connecting a different
account also needs a new test. Tests are limited to one every 30 seconds and 20 per 24 hours, and because they use
the same mailbox they count toward `DAILY_SEND_CAP`, a send to the list waits the usual spacing after one, and a test
waits that spacing after the last email of a send (*wait N more seconds*). While a send to the list is in progress
tests are refused: pause it, send the test, resume it. A failed test shows Microsoft's reason and doesn't unlock
sending; neither does a test whose outcome is unknown (*Not sure whether the test ... went out*: check your inbox
and Sent Items). A send remembers the address its test went to: if a different Outlook account is connected while
it runs, it stops before sending anything more. Connect the original account again and **Resume**, or send a test
of the same message to the new address and then **Resume** (it goes on as the new address), or cancel. The first real test is also the moment to check the headers and
the look in real mail apps: see [docs/email-qa.md](docs/email-qa.md).

### Sending through Microsoft

With `MAIL_TRANSPORT=graph` each email goes to Microsoft Graph `POST /me/sendMail` as base64 MIME
(`app/transport/graph.py`), from the connected address; a copy lands in Sent Items. The `From` display name is the
footer's business name; Outlook.com may show the account's profile name instead (check in the test). If no account
is connected, a send stops at once with "Connect your Outlook account". Each request is given at most 12 seconds
(token renewal included: connect 5 s, read 10 s), so a send in flight at shutdown always finishes in time. When
Microsoft answers that the *account* can't send (401, 403, or codes such as `ErrorMessageSubmissionBlocked`,
`ErrorAccessDenied`, `ErrorSendAsDenied`, `ErrorQuotaExceeded`), the send stops with a message saying what to
do, since every later email would fail too; a refusal of one message (another 4xx, e.g. 400) fails only that
person. Microsoft's own error text and the token are never stored or logged, only its error code.

Each send records the transport it was started with (`MAIL_TRANSPORT`, shown on the send's page and in the list),
and only that transport ever sends it. A send started in `mock` mode that is still unfinished when the app is
switched to `graph` is not sent: its page says "This send was started in mock mode; cancel it and start a new one",
and Resume is refused. Because each upload is sent once, **a mock test run uses up the draft**: upload the CSV again
for the real send.

A background worker in the app sends one email at a time:

- **Pacing**: one message every `60 / SEND_RATE_PER_MINUTE` seconds (10 s by default).
- **Daily limit**: at most `DAILY_SEND_CAP` messages in any rolling 24 hours, counted across all sends. When it is
  reached the send shows *Paused: daily limit reached* with the time it carries on, and resumes by itself.
- **Opt-outs**: `unsubscribed.txt` is checked again just before each email, so someone who unsubscribes during a
  send is skipped (shown as *Skipped (unsubscribed)*). International domains match whether written in Unicode or
  punycode.
- **Retries**: only a failure where the email certainly wasn't accepted is retried: Microsoft couldn't be
  reached, the connection timed out before the email was sent, or the sign-in couldn't be renewed just then. It is
  retried after 30 seconds, 2 minutes and 10 minutes, 3 attempts in all; then it is marked failed. A 429/503 (busy)
  answer is throttling (below). A permanent rejection (another 4xx answer) is not retried. Other people are sent
  to while one waits for a retry.
- **Throttling**: when Microsoft answers 429/503 and says how long to wait (`Retry-After`, 60 s if it doesn't),
  **nothing is sent to anyone** before that time, even after a restart; the send's page shows the time. Being
  asked to wait isn't counted as a failure or as one of the person's 3 attempts, but if it happens 10 times in a
  row without a success the send stops.
- **Stops**: if no account is connected, a different account is connected than the one the send was tested with,
  the Microsoft sign-in has expired or been revoked, Microsoft says the account can't send (blocked, no permission, limit reached), 5 attempts in a row fail, or Microsoft keeps
  throttling, the send stops (*Stopped by an error*) with the reason. Fix the cause (reconnect on the Account page,
  or sign in at outlook.com and clear the block) and press **Resume**. A send you paused stays paused; the reason
  is shown and logged.
- **Exactly once**: each person is marked "sending" in the database before the email is handed to the transport,
  and the result is recorded straight after (retried a few times if the database is busy; nothing else is sent
  until it is saved; after 10 failed tries the send stops, and after a restart that person is reported as
  "interrupted"). If nobody can tell whether the email went out, it is marked failed with "may or may not have
  been delivered; check Sent Items" and is **not** resent automatically (a double email can't be taken back; check
  Sent Items and send it yourself if needed). That happens when the app is stopped or crashes mid-send ("interrupted
  during send — ..."), and when the connection fails after the email was handed over: a timeout waiting for
  Microsoft's answer, the connection dropping mid-answer, or a 500/502/504 server error ("outcome unknown — ...").
  These count against the daily limit and as failed attempts. On a normal shutdown the worker waits up to 15 s for
  an in-flight email to finish first; the Graph request budget (12 s) is shorter than that, and the container's
  stop grace period (`stop_grace_period: 30s` in `compose.yaml`) is longer, so the result is always saved. Docker's
  default is only 10 s: if you run the container another way, give it at least 30 s (`docker stop -t 30`, or
  `--stop-timeout=30` in the Unraid template's Extra Parameters). Everything else carries on after a restart.
- **Pause, resume, cancel**: buttons on the send's page. Cancel keeps what was sent and marks everyone still
  waiting as not sent.

**Sends** in the menu lists every send. A send's page shows the counts by status, a progress bar, the next send
time, errors and the latest 20 events; it updates itself every few seconds (or use **Refresh** without
JavaScript). **Download results (CSV)** gives one row per person: `row_no`, `email`, `first_name`, `status`,
`attempts`, `sent_at` (UTC) and `error` (cells that a spreadsheet would read as a formula are prefixed with `'`).

When a send finishes or is cancelled, its uploaded CSV is deleted; the send's records and results stay. At startup
the app also deletes CSVs of uploads more than 7 days old that were never sent.

## Decisions

The main decisions and the reasons for them are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Setup from a fresh clone

_To be written._

## Configuration

All configuration is through environment variables. The app refuses to start if a required variable is missing or
invalid, and names each one that is wrong.

| Name | Required | Default | Purpose |
|---|---|---|---|
| `PUBLIC_BASE_URL` | yes | none | Public absolute http(s) address, e.g. `https://the-calico-barn.jakewelliott.com`. Used for unsubscribe links and image URLs in emails. A trailing slash is removed. |
| `UNSUBSCRIBE_SECRET` | yes | none | Random string, at least 32 characters. HMAC key for unsubscribe tokens. Changing it breaks links in already-sent emails. |
| `MS_CLIENT_ID` | when `MAIL_TRANSPORT=graph` | none | Entra app (client) ID. Public client, no secret. |
| `MS_AUTHORITY` | no | `https://login.microsoftonline.com/consumers` | Leave as-is for @outlook.com. |
| `CF_ACCESS_TEAM_DOMAIN` | yes unless `AUTH_DISABLED=1` | none | Bare hostname, e.g. `jakewelliott.cloudflareaccess.com`. |
| `CF_ACCESS_AUD` | yes unless `AUTH_DISABLED=1` | none | Application Audience (AUD) tag of the Cloudflare Access app. |
| `AUTH_DISABLED` | no | `0` | `1` turns off the Access check on admin pages. Local testing only; logs a loud warning. |
| `MAIL_TRANSPORT` | no | `graph` | `graph` (real sending) or `mock` (writes `.eml` files to `DATA_DIR/outbox`). |
| `SEND_RATE_PER_MINUTE` | no | `6` | Pacing between messages. Positive whole number. |
| `DAILY_SEND_CAP` | no | `250` | Rolling 24 h cap. Positive whole number. |
| `MAX_RECIPIENTS` | no | `2000` | Upload row limit. Positive whole number. |
| `MAX_UPLOAD_MB` | no | `5` | CSV size limit in MB. Positive whole number. |
| `TOKEN_CACHE_KEY` | no | none | Fernet key to encrypt the Microsoft token cache. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `DATA_DIR` | no | `/data` | Where the database, token cache, opt-out list and mock outbox live. |
| `PORT` | no | `8765` | HTTP port (1 to 65535). |
| `TZ` | no | `America/New_York` | Time zone. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `FORWARDED_ALLOW_IPS` | no | `*` | Comma-separated IPs/CIDRs of the proxy (cloudflared) trusted for `X-Forwarded-For` / `X-Forwarded-Proto`. Tightening it to cloudflared's address or Docker subnet is recommended. Read by uvicorn (the container CMD), not by `config.py`. The client IP uvicorn reports is not trustworthy; see the Dockerfile comment. |

## Development

The tests run in a throwaway Python container, so nothing needs to be installed on the host:

```sh
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -e '.[dev]' && ruff check . && ruff format --check . && pytest -q"
```

To build and run the image locally with the mock transport (writes `.eml` files instead of sending) and the
Cloudflare Access check turned off:

```sh
docker build -t outlook-mailer:dev .
mkdir -p /tmp/om-data && sudo chown 99:100 /tmp/om-data   # the container runs as uid 99, gid 100
docker run --rm -p 8765:8765 -v /tmp/om-data:/data \
  -e PUBLIC_BASE_URL=http://localhost:8765 \
  -e UNSUBSCRIBE_SECRET=change-me-to-a-random-string-of-32-chars \
  -e MAIL_TRANSPORT=mock -e AUTH_DISABLED=1 \
  outlook-mailer:dev
curl http://localhost:8765/healthz   # {"ok":true}
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff and pytest on every pull request and push. Pushes to `main`
and `v*` tags then build the image and push it to `ghcr.io/jakewelliott/outlook-mailer` (`latest` from `main`,
`sha-<short>` always, `1.2.3` and `1.2` from tags). `compose.yaml` runs the published image on Unraid.

## Credits and licenses

_To be written. The email template will be based on [Cerberus](https://github.com/emailmonday/Cerberus) (MIT)._
