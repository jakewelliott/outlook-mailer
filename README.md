# outlook-mailer

A small self-hosted web app for sending a holiday email campaign from a free personal @outlook.com account. Upload a
CSV, map the email and first-name columns, write the message in Markdown, preview it in a Christmas-styled HTML
template, send yourself a test, then run a rate-limited bulk send through Microsoft Graph (one email per recipient,
with a CAN-SPAM footer and a one-click unsubscribe link). It runs as a single Docker container.

> **Status: under construction.** So far: configuration, `/healthz`, the Docker image, the admin UI shell with
> the Cloudflare Access check and the help page, and CSV upload with column mapping and the validation report.
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
