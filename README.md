# outlook-mailer

A small self-hosted web app for sending a holiday email campaign from a free personal @outlook.com account. Upload a
CSV, map the email and first-name columns, write the message in Markdown, preview it in a Christmas-styled HTML
template, send yourself a test, then run a rate-limited bulk send through Microsoft Graph (one email per recipient,
with a CAN-SPAM footer and a one-click unsubscribe link). It runs as a single Docker container.

> **Status: under construction.** Only the scaffold exists so far (configuration and `/healthz`). See
> [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Overview

_To be written._

## How it works

_To be written._

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

## Development

The tests run in a throwaway Python container, so nothing needs to be installed on the host:

```sh
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -e '.[dev]' && ruff check . && ruff format --check . && pytest -q"
```

## Credits and licenses

_To be written. The email template will be based on [Cerberus](https://github.com/emailmonday/Cerberus) (MIT)._
