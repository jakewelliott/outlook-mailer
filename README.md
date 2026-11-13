# outlook-mailer

A small self-hosted web app for sending a holiday email campaign from a free personal @outlook.com account. Upload a
CSV, map the email and first-name columns, write the message in Markdown, preview it in a Christmas-styled HTML
template, send yourself a test, then run a rate-limited bulk send through Microsoft Graph (one email per recipient,
with a CAN-SPAM footer and a one-click unsubscribe link). It runs as a single Docker container behind a Cloudflare
Tunnel and Cloudflare Access.

**Status: v1.0.0.** Feature-complete: CSV upload and mapping, merge fields, Markdown body, Christmas template,
compose and preview, test-send gate, paced bulk sending with pause/resume/cancel, Microsoft sign-in, unsubscribe
pages, the Unraid template and this guide. The automated tests never talk to Microsoft; the first real send is a
manual check ([docs/email-qa.md](docs/email-qa.md)).

## Contents

- [What it does](#what-it-does)
- [Decisions in brief](#decisions-in-brief)
- [Setup from a fresh clone](#setup-from-a-fresh-clone)
- [Secrets: `UNSUBSCRIBE_SECRET` and `TOKEN_CACHE_KEY`](#secrets-unsubscribe_secret-and-token_cache_key)
- [Backups and restoring](#backups-and-restoring)
- [Operating reminders](#operating-reminders)
- [Updating](#updating)
- [Stopping and restarting safely](#stopping-and-restarting-safely)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Development](#development)
- [Credits and licenses](#credits-and-licenses)

More documentation:

| File | What it covers |
|---|---|
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every decision with the reason for it (the full record; this README only summarises). |
| [docs/entra-setup.md](docs/entra-setup.md) | Free Azure account and the Entra app registration (`MS_CLIENT_ID`). |
| [docs/cloudflare.md](docs/cloudflare.md) | Tunnel route, Access application and bypass, security settings, checks. |
| [docs/email-qa.md](docs/email-qa.md) | First real send, step by step, and the mail-app rendering checklist. |
| [docs/PLAN.md](docs/PLAN.md) | The specification the app was built against, with as-built notes. |
| [docs/RESEARCH.md](docs/RESEARCH.md) | Background research (Microsoft limits, Graph, sign-in). |
| [unraid/outlook-mailer.xml](unraid/outlook-mailer.xml) | Unraid Docker template. |
| [compose.yaml](compose.yaml) | Docker Compose file (production). |

## What it does

1. **Sign in to the app** at its public address (`PUBLIC_BASE_URL`). Cloudflare Access asks who you are; nobody
   else gets in.
2. **Connect the Outlook account** once on the **Account** page (a code entered at a Microsoft link).
3. **New send**: upload a CSV (a Square customer export works as-is), pick the email, first-name and (optional)
   subscription-status columns, tick which status values to send to, and read the report of who will and won't be
   emailed and why.
4. **Compose**: subject, preview text, Markdown body with merge fields (`{{first_name|Friend}}`), header text and
   the CAN-SPAM footer (business name, postal address, advertisement notice). Preview it for any recipient, light
   or dark.
5. **Send test to me**: the exact message goes to your own Outlook address. Sending to the list stays locked until
   a test of the current version has gone out.
6. **Start sending**: one email per person, 6 a minute and at most 250 a day by default. Pause, resume or cancel;
   it carries on after a restart and never sends anyone the same email twice.
7. Recipients who click **Unsubscribe** (or their mail app's own unsubscribe button) are added to
   `unsubscribed.txt`; the **Unsubscribes** page lists them so you can remove them from your customer list.

## Decisions in brief

The full table with the reasons is [docs/DECISIONS.md](docs/DECISIONS.md). The major ones:

- **Sending account: a free personal @outlook.com account, nothing paid.** Microsoft turned off password and
  app-password SMTP for Outlook.com in September 2024, so the only way left is **Microsoft Graph `sendMail` with
  delegated OAuth**. The app registration lives in the free Entra directory that comes with a free Azure account
  (personal accounts can't register apps outside a directory any more).
- **Sign-in: device code.** The app shows a code; you enter it at Microsoft's link (normally microsoft.com/link)
  on any device. No password or client secret is stored. The token renews itself; you reconnect only after a
  password change, removing the app's access, or about 90 days unused.
- **Message format: MIME through Graph.** Each email is `multipart/alternative` (plain text and HTML) with
  `List-Unsubscribe` and `List-Unsubscribe-Post` headers, sent as base64 MIME. Copies go to Sent Items. One
  recipient per message; never Cc or Bcc.
- **Commercial email (CAN-SPAM).** The footer always has the business name, a postal address, "This message is an
  advertisement" (on by default), why the person is getting it, and an unsubscribe link.
- **Unsubscribe.** The footer link opens a page that asks the person to **type the address the email was sent
  to** and press one button (so link scanners and people a newsletter was forwarded to don't unsubscribe anyone by
  accident). Mail apps' own unsubscribe buttons use the RFC 8058 **one-click** POST, which needs nothing typed.
  Opt-outs are appended to `unsubscribed.txt` (`email<TAB>time`); every recipient is checked against it again just
  before their email goes out. Links are HMAC-signed with `UNSUBSCRIBE_SECRET`, never expire and must keep working
  for at least 30 days after a send. There is no reply-to-unsubscribe. You remove opted-out people from your CSV
  yourself; the app also skips them.
- **Pacing: 6 per minute, 250 per rolling 24 hours** (configurable). Microsoft doesn't publish free-account limits
  (about 300 a day is reported), and staying under them avoids the account being flagged.
- **Test-send gate.** Bulk sending needs an accepted test of the exact content (by hash) to the signed-in address;
  any edit, or connecting another account, needs a new test. Tests count toward the daily cap.
- **Access model.** Cloudflare Tunnel plus a Cloudflare Access application decide who can use the app. `/u/*`
  (unsubscribe), `/img/*` (email images) and `/healthz` are public and need a Bypass in Access. The app also checks
  the Access JWT on every other request, so the LAN port can't be used to get round Cloudflare. No separate app
  login.
- **Data kept.** The uploaded CSV is deleted when its send finishes or is cancelled; the send log and results are
  kept.
- **Port 8765**, free on the server.
- **Public repo and public image** (`github.com/jakewelliott/outlook-mailer`,
  `ghcr.io/jakewelliott/outlook-mailer`). Neither holds secrets; a public image lets Unraid pull without logging
  in to GHCR.
- **Stack:** Python 3.12, FastAPI, Jinja2, SQLite, MSAL. No frontend build. One process, one send worker.

## Setup from a fresh clone

Everything needed to rebuild from nothing, in order. Allow an hour or two, most of it the Azure sign-up and
Cloudflare. The container image is built by GitHub Actions; the server only pulls it, so you don't need to build
anything.

### 0. What you need

- The @outlook.com account the emails will come from (password and two-step verification working).
- A domain on Cloudflare (here `jakewelliott.com`) and a running `cloudflared` tunnel (on Jake's server it is its
  own container).
- An Unraid server (or any Docker host) with a fixed LAN IP.
- A password manager for the two secrets.
- Optional: a clone of this repository, for the docs and the Unraid template:
  `git clone https://github.com/jakewelliott/outlook-mailer.git`. On the server you only need the template (or
  `compose.yaml`).

### 1. Microsoft: free Azure account and app registration

Follow [docs/entra-setup.md](docs/entra-setup.md). You end up with the **Application (client) ID**, which is
`MS_CLIENT_ID`. There is no client secret. **Allow public client flows** must be on; step 6 below proves it.

### 2. Secrets

Generate `UNSUBSCRIBE_SECRET` and `TOKEN_CACHE_KEY` (commands in
[Secrets](#secrets-unsubscribe_secret-and-token_cache_key)) and save both in your password manager **before**
putting them in the container. If you are rebuilding after losing the server, **use the saved values, don't
generate new ones**: a new `UNSUBSCRIBE_SECRET` breaks every unsubscribe link already sent.

### 3. Cloudflare tunnel and Access

Follow [docs/cloudflare.md](docs/cloudflare.md): the tunnel route to `http://<server-LAN-IP>:8765`, the Access
application with an Allow policy for you, the Bypass for `/u/*`, `/img/*` and `/healthz`, Bot Fight Mode off and
no challenges on `/u/*`. Note the **team domain** (`CF_ACCESS_TEAM_DOMAIN`) and the **AUD tag**
(`CF_ACCESS_AUD`).

### 4. Data folder

The container runs as uid 99 / gid 100 (Unraid's `nobody:users`). Create the appdata folder with that owner
before the first start (if Docker creates it, it belongs to root and the app can't write to it):

```sh
mkdir -p /mnt/user/appdata/outlook-mailer
chown 99:100 /mnt/user/appdata/outlook-mailer
chmod 700 /mnt/user/appdata/outlook-mailer
```

When restoring from a backup, put the files back now (see [Backups and restoring](#backups-and-restoring)).

### 5a. Deploy on Unraid (template)

1. On the Unraid server (terminal or SSH), download the template into Unraid's user templates folder:

   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-outlook-mailer.xml \
     https://raw.githubusercontent.com/jakewelliott/outlook-mailer/main/unraid/outlook-mailer.xml
   ```

   (Or copy `unraid/outlook-mailer.xml` from a clone to that folder as `my-outlook-mailer.xml`. Do this only on a
   fresh server: if `my-outlook-mailer.xml` already exists, it holds your saved values and this overwrites it.)
2. In the web UI, go to **Docker** > **Add Container**, and under **Template** pick **outlook-mailer** (in the
   user templates group).
3. Fill in the required fields: `PUBLIC_BASE_URL`, `UNSUBSCRIBE_SECRET`, `MS_CLIENT_ID`,
   `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, and (recommended) `TOKEN_CACHE_KEY`. The secrets are masked. Leave
   the rest (under **Show more settings**) at their defaults.
4. Check that **Extra Parameters** (advanced view) still says `--stop-timeout=30`, the port is `8765` and
   **Appdata** is `/mnt/user/appdata/outlook-mailer`.
5. The template's **WebUI** link is `https://the-calico-barn.jakewelliott.com/`, because the admin pages only work
   through Cloudflare Access (on the LAN port they say *Access denied*). Change it in the advanced view if your
   address is different.
6. **Apply**. Unraid pulls `ghcr.io/jakewelliott/outlook-mailer:latest` and starts it. To pin a release instead,
   set **Repository** to `ghcr.io/jakewelliott/outlook-mailer:1.0.0` (image tags have no `v`; see
   [Updating](#updating)).

Unraid saves the values in `/boot/config/plugins/dockerMan/templates-user/my-outlook-mailer.xml` on the flash
drive, in plain text. That is normal for Unraid, but it's another reason to keep the secrets in a password manager
as well (the flash drive can die too). Never commit that file.

If the container stops right away, its log (Docker tab > container icon > **Logs**) says which variable is missing
or wrong: `outlook-mailer refused to start. Invalid configuration: ...`.

### 5b. Or deploy with Docker Compose

[compose.yaml](compose.yaml) is for **production only**: it uses `${VAR:?}`, so Compose refuses to start unless
`PUBLIC_BASE_URL`, `UNSUBSCRIBE_SECRET`, `MS_CLIENT_ID`, `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` are all set,
even if you only wanted mock mode or `AUTH_DISABLED=1` (for local testing use the `docker run` command in
[Development](#development)). It already sets `stop_grace_period: 30s`, the port and the appdata path.

```sh
mkdir -p /mnt/user/appdata/outlook-mailer-compose && cd /mnt/user/appdata/outlook-mailer-compose
wget https://raw.githubusercontent.com/jakewelliott/outlook-mailer/main/compose.yaml
touch .env && chmod 600 .env    # then put the values in it (below)
docker compose pull && docker compose up -d
```

`.env` (keep it out of the data folder and out of git):

```sh
PUBLIC_BASE_URL=https://the-calico-barn.jakewelliott.com
UNSUBSCRIBE_SECRET=<from your password manager>
MS_CLIENT_ID=<Application (client) ID>
CF_ACCESS_TEAM_DOMAIN=jakewelliott.cloudflareaccess.com
CF_ACCESS_AUD=<AUD tag>
TOKEN_CACHE_KEY=<from your password manager>
```

`TOKEN_CACHE_KEY` is commented out in `compose.yaml`: uncomment its line there so the value is passed in. On
Unraid, Compose needs the Docker Compose Manager plugin (or the compose CLI plugin).

### 6. First sign-in to Outlook

Open `PUBLIC_BASE_URL`, sign in through Access, and go to **Account** > **Connect Outlook account**. Open the link
shown, enter the code, sign in with the @outlook.com account and **Accept**. The page then shows *Sending as
you@outlook.com*. This also proves **Allow public client flows** is on in the app registration: if it isn't, the
page shows `AADSTS7000218` (fix in [docs/entra-setup.md](docs/entra-setup.md#troubleshooting)).

### 7. Optional: a mock dry run

To try the whole flow without sending anything, set `MAIL_TRANSPORT=mock` and restart. Emails are then written to
`/mnt/user/appdata/outlook-mailer/outbox/` as `.eml` files. A mock run is gated like a real one: upload a CSV
(use **your own addresses**: the unsubscribe links in the `.eml` files are real and would really unsubscribe
whoever's address it is), compose, **Send test to me** (it goes to the outbox, addressed to `you@example.com`),
then **Start sending**. Each upload is sent once, so **the dry run uses up that draft**: after switching back to
`MAIL_TRANSPORT=graph` and restarting, upload the CSV again for the real send (a send started in mock mode is
never sent through Graph). Delete the `outbox/` files when done.

### 8. First test and real send

Follow [docs/email-qa.md](docs/email-qa.md): a draft to your own addresses, **Send test to me**, checking headers
and rendering in Outlook.com and Gmail, then a tiny real send. Only then upload the customer list.

### 9. Post-deploy checks

From a computer not signed in to Access (what each failure means:
[docs/cloudflare.md](docs/cloudflare.md#6-checks-after-setup)):

```sh
curl -i https://the-calico-barn.jakewelliott.com/healthz               # 200 {"ok":true}, no login
curl -i https://the-calico-barn.jakewelliott.com/u/x                   # 404 "This link isn't valid" (the app)
curl -i -X POST https://the-calico-barn.jakewelliott.com/u/x/one-click # the same 404 page, not Access
curl -sI https://the-calico-barn.jakewelliott.com/img/holly-v1.png     # 200 image/png, no login
curl -i https://the-calico-barn.jakewelliott.com/                      # 302 to the Access login
```

On the server:

```sh
docker inspect -f '{{.Config.StopTimeout}}' outlook-mailer   # 30
docker inspect -f '{{.State.Health.Status}}' outlook-mailer  # healthy
ls -ln /mnt/user/appdata/outlook-mailer                      # app.db and friends owned by 99 100
docker logs outlook-mailer 2>&1 | grep -i warn               # no CF-Connecting-IP or TOKEN_CACHE_KEY warnings
curl -i http://localhost:8765/                               # 403 Access denied (the app's own JWT check)
```

The 30 s only applies to Unraid's Restart and a plain `docker stop`: Unraid's Stop, updates and array stop use the
global Docker Stop Timeout instead (see [Stopping and restarting safely](#stopping-and-restarting-safely)).

In the app: **Account** shows the connected address (and no warning about an unencrypted token cache if
`TOKEN_CACHE_KEY` is set); the home page lists the steps.

## Secrets: `UNSUBSCRIBE_SECRET` and `TOKEN_CACHE_KEY`

Both are generated once and then kept for good. The commands work on Unraid as is (`openssl` is part of Unraid; no
Python needed on the host):

```sh
openssl rand -hex 32                        # UNSUBSCRIBE_SECRET: 64 hex characters
openssl rand -base64 32 | tr '+/' '-_'      # TOKEN_CACHE_KEY: 44 characters ending in "="
```

Without `openssl`, the same with a throwaway Python container:

```sh
docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_hex(32))"
docker run --rm python:3.12-slim python -c \
  "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

| | `UNSUBSCRIBE_SECRET` | `TOKEN_CACHE_KEY` |
|---|---|---|
| Required | yes | no, but recommended |
| Format (checked at start, `app/config.py`) | any string of at least 32 characters (surrounding spaces are trimmed first) | a Fernet key: 32 random bytes, url-safe base64 (`A-Z a-z 0-9 - _`), 44 characters with the final `=` |
| Used for | the HMAC in every unsubscribe link, and admin form (CSRF) tokens | encrypting the saved Microsoft sign-in (`msal_cache.json`) |
| If it changes or is lost | **every unsubscribe link in emails already sent stops working** (people get *This link isn't valid*, a CAN-SPAM problem for 30 days after each send); open admin forms need a reload | the saved sign-in can't be read: connect Outlook again on the Account page (putting the old key back also works) |

Store both in a password manager, off the server. They also end up in Unraid's saved template on the flash drive
(or in the compose `.env`), but those die with the server. Never put them in the repository; the image and the
repo are public.

## Backups and restoring

Everything the app keeps is in the appdata folder (`/mnt/user/appdata/outlook-mailer`, `/data` in the container):

| Path | What | Back up? |
|---|---|---|
| `unsubscribed.txt` | Every opt-out, `email<TAB>time`, appended by the app. | **Essential.** It's the legal record of who opted out (CAN-SPAM): lose it and people who unsubscribed could be emailed again. Also download it from the **Unsubscribes** page after each campaign. |
| `app.db` (plus `app.db-wal`, `app.db-shm` while running) | SQLite: sends, per-recipient results and the send log (the record of what went out), drafts, saved footer and header settings, test-send records, the signed-in address. | **Yes.** Copy all three files together, with the container stopped. |
| `msal_cache.json` | The Microsoft sign-in (encrypted with `TOKEN_CACHE_KEY`). | Optional: without it you just connect Outlook again. Useless without the key. |
| `uploads/` | Uploaded CSVs, deleted when their send ends (or after 7 days if never sent). | No. It's customer data that is meant to be deleted. |
| `outbox/` | `.eml` files from mock mode. | No. |

Application logs go to the container's output (`docker logs outlook-mailer`), not to a file, and are lost when the
container is recreated; the send log in `app.db` is the lasting record.

A simple backup (stopping the container for a few seconds; pause a running send first):

```sh
mkdir -p /mnt/user/backups
docker stop -t 30 outlook-mailer
tar -czf /mnt/user/backups/outlook-mailer-$(date +%F).tar.gz -C /mnt/user/appdata outlook-mailer
docker start outlook-mailer
```

The Unraid **Appdata Backup** plugin does the same on a schedule (it stops containers while copying). Keep a copy
off the server too.

**Restoring after losing the server:** follow the setup steps with the **same** `UNSUBSCRIBE_SECRET` and
`TOKEN_CACHE_KEY` from your password manager and the same `PUBLIC_BASE_URL`. Before the first start, unpack the
backup into `/mnt/user/appdata/outlook-mailer` and run `chown -R 99:100 /mnt/user/appdata/outlook-mailer`. If the
Cloudflare Access application had to be recreated, its AUD tag is new: update `CF_ACCESS_AUD`. If the Entra app
registration was lost, create a new one and update `MS_CLIENT_ID`, then connect Outlook again.

## Operating reminders

- **Around day 30 after the Azure sign-up** the free trial subscription is disabled (expected, nothing to do).
  Microsoft doesn't promise that personal-account sign-in to the app keeps working after that, so **do a test send
  then** to confirm it still does.
- **Sign in to the [Entra admin center](https://entra.microsoft.com) every couple of months** and open the app
  registration. Microsoft blocks directories it considers inactive and deletes them 20 days later, along with the
  app registration.
- **Keep the container and the tunnel running for at least 30 days after every send.** Unsubscribe links must keep
  working that long (CAN-SPAM), and the email images are served by the app. Don't change `PUBLIC_BASE_URL`,
  `UNSUBSCRIBE_SECRET` or the Access bypass in that time either.
- **Use a real postal address in the footer**: your street address, or a USPS PO box (or a registered private
  mailbox) if you don't want to publish it. CAN-SPAM requires a valid physical postal address.
- **Remove opted-out addresses from your own customer list** after each campaign, using the **Unsubscribes** page.
  The app also skips anyone in `unsubscribed.txt` at send time, so a missed one isn't emailed, but your CSV (and
  Square) should be updated too.
- **Back up `unsubscribed.txt`** after every campaign.
- **If sending stops** with an account message (blocked, unusual activity, limit reached), sign in at outlook.com,
  follow its prompts, and resume later, perhaps with a lower `SEND_RATE_PER_MINUTE` / `DAILY_SEND_CAP`
  ([troubleshooting](docs/entra-setup.md#troubleshooting)).
- **About 90 days without using the app** the Microsoft sign-in expires: reconnect on the Account page before the
  next campaign.

## Updating

GitHub Actions builds the image on every push to `main` and on every `v*` tag:

| Image tag | Comes from |
|---|---|
| `latest` | the newest commit on `main` |
| `1.0.0`, `1.0` | the git tag `v1.0.0` (the image tags have no `v`) |
| `sha-<short commit>` | every build |

To update a container that follows `latest`: on Unraid, Docker tab > **Check for updates** > **apply update**;
with Compose, `docker compose pull && docker compose up -d`. To stay on a release, set the image to
`ghcr.io/jakewelliott/outlook-mailer:1.0.0` and change the tag when you choose to update.

**Pause a running send before updating.** On Unraid an update stops the container with the global Docker Stop
Timeout (default 10 s), not the template's 30 s, so an email in flight can be cut off (see below). Resume it
afterwards. Updates keep the data folder; database changes are applied automatically at start.

## Stopping and restarting safely

When the container is told to stop, the send worker starts no new email and waits up to 15 s
(`SHUTDOWN_GRACE_SECONDS`) for the one in flight. The timeouts are ordered so that email always ends (sent or
timed out) and its result is saved before Docker kills the process:

**Graph request budget 12 s < `SHUTDOWN_GRACE_SECONDS` 15 s < container stop grace period 30 s.**

At the same time (not one after the other) the app ends a Microsoft sign-in in progress, waiting up to 3 s for it,
or up to 12 s if it is already being saved, so the whole shutdown takes at most about 15 s.

**The habit to keep: pause a running send before you stop the container, update it, edit and Apply its
settings, stop the array or reboot the server.** A paused send has nothing in flight, so it doesn't matter how long Docker waits, and it resumes
where it left off.

Docker's default stop grace period is only 10 s, so:

- Unraid: the template's **Extra Parameters** has `--stop-timeout=30`. Keep it, but it only covers Unraid's
  **Restart** and a plain `docker stop outlook-mailer`. Unraid's **Stop** button, **apply update** and stopping
  the array pass their own timeout to Docker, the global **Settings > Docker > Docker Stop Timeout** (default
  10 s), and that overrides `--stop-timeout`. With the default, those kill the container after 10 s, before the
  app's 15 s grace is up.
- Compose: `stop_grace_period: 30s` in `compose.yaml`.
- By hand: `docker stop -t 30 outlook-mailer`.

If you'd rather not depend on pausing, you can set **Docker Stop Timeout** to 30 or more. That setting is for the
whole server, not this container: every other container also gets up to that long to stop when the array stops
(or is stopped or updated from the Docker tab), so array stop can take longer. On Unraid 7.3.1 the field is shown
and can be changed while Docker is running, and the value is read at each stop, so it takes effect as soon as you
press **Apply**, without restarting Docker. It's optional; pausing first is enough.

If the process is killed mid-send anyway (a 10 s stop, power cut, crash), nothing is sent twice: that one
recipient is marked *interrupted during send — may or may not have been delivered; check Sent Items* and is never
resent automatically.

## Configuration

All configuration is through environment variables (`app/config.py`). The app refuses to start if a required
variable is missing or invalid, and names each one that is wrong. The Unraid template lists all of them; in
`compose.yaml` the optional ones are commented out with their defaults.

| Name | Required | Default | Purpose |
|---|---|---|---|
| `PUBLIC_BASE_URL` | yes | none | Public absolute http(s) address, e.g. `https://the-calico-barn.jakewelliott.com`. Used for unsubscribe links, image URLs and the `Message-ID` host. A trailing slash is removed; no query or fragment; a host name, not an IPv6 address. |
| `UNSUBSCRIBE_SECRET` | yes | none | Random string, at least 32 characters. HMAC key for unsubscribe tokens (and admin form tokens). Changing it breaks links in already-sent emails. See [Secrets](#secrets-unsubscribe_secret-and-token_cache_key). |
| `MS_CLIENT_ID` | when `MAIL_TRANSPORT=graph` (the default) | none | Entra app (client) ID. Public client, no secret. |
| `MS_AUTHORITY` | no | `https://login.microsoftonline.com/consumers` | Leave as-is for @outlook.com. |
| `CF_ACCESS_TEAM_DOMAIN` | yes unless `AUTH_DISABLED=1` | none | Bare hostname, e.g. `jakewelliott.cloudflareaccess.com`. |
| `CF_ACCESS_AUD` | yes unless `AUTH_DISABLED=1` | none | Application Audience (AUD) tag of the Cloudflare Access app. |
| `AUTH_DISABLED` | no | `0` | `0` or `1`. `1` turns off the Access check on admin pages. Local testing only; logs a loud warning. |
| `MAIL_TRANSPORT` | no | `graph` | `graph` (real sending) or `mock` (writes `.eml` files to `DATA_DIR/outbox`). |
| `SEND_RATE_PER_MINUTE` | no | `6` | Pacing between messages. Positive whole number. |
| `DAILY_SEND_CAP` | no | `250` | Rolling 24 h cap, tests included. Positive whole number. |
| `MAX_RECIPIENTS` | no | `2000` | Upload row limit. Positive whole number. |
| `MAX_UPLOAD_MB` | no | `5` | CSV size limit in MB. Positive whole number. |
| `TOKEN_CACHE_KEY` | no (recommended) | none | Fernet key that encrypts the Microsoft token cache. Generate with `openssl rand -base64 32 \| tr '+/' '-_'`; see [Secrets](#secrets-unsubscribe_secret-and-token_cache_key). |
| `DATA_DIR` | no | `/data` | Where the database, token cache, opt-out list, uploads and mock outbox live. |
| `PORT` | no | `8765` | HTTP port inside the container (1 to 65535). Change the port mapping and the tunnel too if you change it. |
| `TZ` | no | `America/New_York` | Time zone. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `FORWARDED_ALLOW_IPS` | no | `*` | Comma-separated IPs/CIDRs of the proxy (cloudflared) trusted for `X-Forwarded-For` / `X-Forwarded-Proto`. Tightening it to cloudflared's address or Docker subnet is possible. Read by uvicorn (the container CMD), not by `config.py`. The client IP uvicorn reports is not trustworthy; see the Dockerfile comment. |

Blank values count as unset. `compose.yaml` additionally requires `MS_CLIENT_ID`, `CF_ACCESS_TEAM_DOMAIN` and
`CF_ACCESS_AUD` in every case (it is the production file).

## How it works

### Access

The app is reached through a Cloudflare tunnel and protected by a Cloudflare Access application, which decides who
can sign in. Everything is protected except `/u/*` (unsubscribe links), `/img/*` (images in emails) and `/healthz`;
those need a Bypass policy in Access because email recipients and health checks must reach them without signing in.
The app also checks the Access JWT itself (the `Cf-Access-Jwt-Assertion` header: RS256, Cloudflare's published keys,
audience and issuer) on every other request, so the admin pages can't be used by going to the container's port
directly. Forms carry a CSRF token and POSTs must come from `PUBLIC_BASE_URL`. Cloudflare setup:
[docs/cloudflare.md](docs/cloudflare.md).

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
Unsubscribe links are `PUBLIC_BASE_URL/u/<token>` (the `List-Unsubscribe` header adds `/one-click`), where the
token is the address (base64url) plus a truncated HMAC-SHA256 made with `UNSUBSCRIBE_SECRET`
(`app/unsubscribe_tokens.py`).

### Messages

Each recipient gets their own email (`app/message.py`): a standard MIME message with a plain-text part followed by
the HTML part, both UTF-8 (quoted-printable, or base64 for text that is mostly non-Latin), addressed to that one
person in `To` and nobody else (no `Cc`, `Bcc` or `Reply-To`). Headers: `From`, `To` (with a display name, safely
quoted or encoded), `Subject`, `Date`, `Message-ID`, `List-Unsubscribe: <PUBLIC_BASE_URL/u/<token>/one-click>`
and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, so mail apps that support it (Gmail, Apple Mail,
Outlook.com) can show their own unsubscribe button. A header value containing a line break or control character
is refused. Microsoft Graph gets the message base64-encoded. With `MAIL_TRANSPORT=mock` nothing is sent: each
message is written to `DATA_DIR/outbox/<time>-<n>-<recipient>.eml` (readable only by the app), which opens in any
mail app, and the `From` is the placeholder `sender@example.invalid`.

### Microsoft sign-in

Real sending needs the Outlook.com account connected once, on the **Account** page (first set up the app
registration in [docs/entra-setup.md](docs/entra-setup.md) and set `MS_CLIENT_ID`). **Connect Outlook account**
shows a short code and the link Microsoft returns (normally `https://www.microsoft.com/link` for personal
accounts; the app shows whatever Microsoft sends, never a fixed address). Open the link on any device, enter the
code within 15 minutes, sign in with the @outlook.com account and select **Accept**. The page updates by itself
(or use **Refresh**) and then shows *Sending as you@outlook.com (Your Name)*; that address is the `From` of every
email. Only one sign-in runs at a time and it can be cancelled; reconnecting and then cancelling keeps the
existing sign-in. Work or school accounts are refused. The menu shows whether the account is connected (or
needs connecting again). After Microsoft refuses a token (HTTP 401) the app asks Microsoft for a fresh one on the
next send instead of reusing the saved one; *reconnect needed* goes away only when that renewal succeeds, a send
is accepted, or you connect again.

The app asks for `Mail.Send` and `User.Read` (MSAL adds `offline_access`, which lets it renew access by itself).
The saved sign-in (MSAL's token cache) is `DATA_DIR/msal_cache.json`, readable only by the app (mode 0600) and
replaced atomically. With `TOKEN_CACHE_KEY` set it is encrypted (Fernet); without it the file is plain JSON and the
Account page recommends setting a key. Setting a key later encrypts the existing file at the next start. If the key
changes or is removed, the saved sign-in can't be read: the page says so and the account has to be connected again
(nothing crashes; putting the old key back also works). The sending address is stored in the database, separately
from that file; at every start the app checks that the saved sign-in is for that address's account. If they don't
match, or only one of them is there (e.g. the container was stopped while a sign-in was being saved), the app
doesn't use the saved sign-in at all: the Account and home pages say to connect again, and nothing can be sent
until you do. Tokens and codes are never logged.

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
of the same message to the new address and then **Resume** (it goes on as the new address), or cancel. The first
real test is also the moment to check the headers and the look in real mail apps: see
[docs/email-qa.md](docs/email-qa.md).

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
  answer is throttling (below). A permanent rejection (another 4xx answer) is not retried, and neither is a
  500/502/504 server error, whatever it says (see Exactly once). Other people are sent to while one waits for a
  retry.
- **Throttling**: when Microsoft answers 429/503 and says how long to wait (`Retry-After`, 60 s if it doesn't),
  **nothing is sent to anyone** before that time, even after a restart; the send's page shows the time. Being
  asked to wait isn't counted as a failure or as one of the person's 3 attempts, but if it happens 10 times in a
  row without a success the send stops.
- **Stops**: the send stops (*Stopped by an error*) with the reason if no account is connected, a different
  account is connected than the one the send was tested with, the Microsoft sign-in has expired or been revoked,
  Microsoft says the account can't send (blocked, no permission, limit reached), 5 attempts in a row fail, or
  Microsoft keeps throttling. If Microsoft says the account can't send but answers with a server error
  (500/502/504), the email it was sending may or may not have gone out: that person is marked failed with
  "check Sent Items" and is not sent again on resume (see Exactly once). Fix the cause (reconnect on the Account
  page, or sign in at outlook.com and clear the block) and press **Resume**. A send you paused stays paused; the
  reason is shown and logged.
- **Exactly once**: each person is marked "sending" in the database before the email is handed to the transport,
  and the result is recorded straight after (retried a few times if the database is busy; nothing else is sent
  until it is saved; after 10 failed tries the send stops, and after a restart that person is reported as
  "interrupted"). If nobody can tell whether the email went out, it is marked failed with "may or may not have
  been delivered; check Sent Items" and is **not** resent automatically (a double email can't be taken back; check
  Sent Items and send it yourself if needed). That happens when the app is stopped or crashes mid-send ("interrupted
  during send — ..."), and when the connection fails after the email was handed over: a timeout waiting for
  Microsoft's answer, the connection dropping mid-answer, or a 500/502/504 server error ("outcome unknown — ..."),
  even when that error also says the account can't send (the send then stops as well). Among Microsoft's answers, only a
  refusal (a 4xx, or 429/503/509 busy) is taken as proof that nothing went out; failures before the request is sent
  (can't connect, can't get a token) also mean nothing went out, and are retried.
  These count against the daily limit and as failed attempts. On a normal shutdown the worker waits up to 15 s for
  an in-flight email to finish first (see [Stopping and restarting safely](#stopping-and-restarting-safely)).
  Everything else carries on after a restart.
- **Pause, resume, cancel**: buttons on the send's page. Cancel keeps what was sent and marks everyone still
  waiting as not sent.

**Sends** in the menu lists every send. A send's page shows the counts by status, a progress bar, the next send
time, errors and the latest 20 events; it updates itself every few seconds (or use **Refresh** without
JavaScript). **Download results (CSV)** gives one row per person: `row_no`, `email`, `first_name`, `status`,
`attempts`, `sent_at` (UTC) and `error` (cells that a spreadsheet would read as a formula are prefixed with `'`).

When a send finishes or is cancelled, its uploaded CSV is deleted; the send's records and results stay. At startup
the app also deletes CSVs of uploads more than 7 days old that were never sent.

### Unsubscribe

The footer of every email links to `PUBLIC_BASE_URL/u/<token>`; the `List-Unsubscribe` header carries
`PUBLIC_BASE_URL/u/<token>/one-click`. The token holds the address and an HMAC made with `UNSUBSCRIBE_SECRET`, so it
can't be guessed or changed to another address, and it never expires: it doesn't depend on any send, draft or
uploaded file, so links keep working long after a send (CAN-SPAM needs at least 30 days) as long as
`UNSUBSCRIBE_SECRET` stays the same.

- **Opening the link** shows a small page that asks for the email address the message was sent to, with one
  **Unsubscribe** button. It never shows the address (or the business name). Opening it records nothing, so mail
  scanners that follow links don't unsubscribe anyone.
- **Typing the address and pressing the button** records the opt-out and shows *You're unsubscribed*. Capitals,
  spaces around it, and an international domain typed in Unicode or punycode don't matter. A different (or empty)
  address shows the form again (HTTP 400) with *That email address doesn't match* and asks them to look at the
  original email for the address it was sent to; what they typed isn't shown back. This step is there so that
  someone a newsletter was forwarded to doesn't unsubscribe the original recipient by accident; it isn't a security
  measure (the link itself is what allows unsubscribing).
- **A mail app's own unsubscribe button** (Gmail, Apple Mail, Outlook.com) sends the RFC 8058 one-click `POST` to
  the `/one-click` address; that records the opt-out with nothing typed. Opening the `/one-click` address in a
  browser shows the normal confirmation page instead. Doing either again shows the same page and adds nothing.
- A changed or cut-off link shows *This link isn't valid* (HTTP 404) and says nothing about the address.
- If `unsubscribed.txt` can't be written (or stays locked for 10 seconds) the page says *Something went wrong, try
  again* (HTTP 503 with `Retry-After`), never that you're unsubscribed.
- **Rate limits** (*Too many requests*, HTTP 429 with `Retry-After`): opening the page, invalid links and mistyped
  addresses count against the visitor's IP: 20 at once, then one every 3 seconds. Mistyped addresses also count
  per address (5, then one a minute). A valid opt-out (one-click, or the right address typed) is never refused
  because of the IP, since Gmail and Outlook.com send everyone's one-click requests from a few of their own servers;
  it is only limited per address, and only once that address is already recorded (repeats beyond 5, then one a
  minute), so the first opt-out for an address always goes through.
- Visitors are told apart by Cloudflare's `CF-Connecting-IP` header, never by `X-Forwarded-For`. **Cloudflare must
  pass `CF-Connecting-IP`**: don't turn on the *Remove visitor IP headers* managed transform. Without it the app
  only sees cloudflared's address, so all visitors would share one limit; the app logs a warning (at most once an
  hour) when an unsubscribe request arrives without the header. Requests to the LAN port directly never have it.
- These pages need no sign-in: Cloudflare Access needs a **Bypass** for `/u/*` (as for `/img/*` and `/healthz`),
  and it must cover `/u/<token>/one-click` too ([docs/cloudflare.md](docs/cloudflare.md#bypass-for-the-public-paths)).
  The `curl` checks in [Post-deploy checks](#9-post-deploy-checks) must show the app's own *This link isn't valid*
  page (HTTP 404) for both `GET /u/x` and `POST /u/x/one-click`, not a Cloudflare sign-in page, a redirect (302) to
  `cloudflareaccess.com`, or a challenge page. The app itself never asks for the Access token on them, and they use
  no cookies. They have their own minimal styling and a strict Content-Security-Policy (no scripts, no images,
  inline CSS allowed only by its hash, forms may post only to this site, no framing), and are never cached.
- **No Cloudflare challenges on `/u/*`**: Gmail and Outlook.com send the one-click `POST` from their servers and
  can't solve a challenge, so those opt-outs would be lost without anyone noticing. **Bot Fight Mode must be off**
  (no rule can exempt a path from it); Super Bot Fight Mode, WAF challenges, Browser Integrity Check and *I'm Under
  Attack* mode must be skipped or turned off for `/u/*` with a skip rule and a configuration rule
  ([docs/cloudflare.md](docs/cloudflare.md#3-security-settings-that-must-not-block-u)).
- The form's body is limited to 4 KB and the typed address to 320 bytes; anything bigger is treated as a mistyped
  address (HTTP 400). The one-click address never reads the request body.

The opt-out is appended to `DATA_DIR/unsubscribed.txt` as `email<TAB>time` (e.g.
`ada@example.com<TAB>2026-12-01T10:00:05-05:00`, in `TZ`), once per address: an address already in the file in any
capitalisation, or with its international domain written in Unicode or punycode, isn't added again. Writes take a
file lock (waiting at most 10 seconds for another writer) and are flushed to disk before the page says you're
unsubscribed; a hand-edited file whose last line has no newline, or that starts with a byte-order mark (Windows
Notepad), is handled. A running send rereads the file before every email (comparing its content, so even a hand edit
that keeps the size and time is noticed) and skips anyone in it; if it can't read the file it sends nothing and tries
again later. You can add lines by hand (an address on its own is enough); edit the file only while the app is
stopped, or append without changing existing lines.

The **Unsubscribes** page in the menu lists the file's entries with when they unsubscribed (newest first), the
number of different addresses (an address listed twice, e.g. in other capitals, counts once; the page then also
says how many entries the file has), and **Download unsubscribed.txt** for the file as it is (an empty file if nobody
has unsubscribed). Use it to remove those people from your customer list. Like every admin page it is behind
Cloudflare Access.

## Development

The tests (about 1,175) run in a throwaway Python container, so nothing needs to be installed on the host. From the
repository root (takes 5 to 10 minutes):

```sh
docker run --rm -v "$PWD":/src:ro python:3.12-slim sh -c "cp -r /src /work && useradd -m t && chown -R t /work \
  && pip install -q --root-user-action=ignore -e '/work[dev]' \
  && su t -c 'cd /work && ruff check . && ruff format --check . && pytest -q -p no:cacheprovider'"
```

The tests run as a normal user, like GitHub Actions does: as root, a test that writes somewhere only root can (such
as the default `DATA_DIR=/data`) passes locally and then fails in CI. The repository is mounted read-only and copied
into the container, so nothing is left behind in the working tree.

To build and run the image locally with the mock transport (writes `.eml` files instead of sending) and the
Cloudflare Access check turned off (`compose.yaml` can't do this; it is production-only):

```sh
docker build -t outlook-mailer:dev .
mkdir -p data && sudo chown 99:100 data   # gitignored; the container runs as uid 99, gid 100 (no sudo as root)
docker run --rm -p 8765:8765 -v "$PWD/data":/data \
  -e PUBLIC_BASE_URL=http://localhost:8765 \
  -e UNSUBSCRIBE_SECRET=change-me-to-a-random-string-of-32-chars \
  -e MAIL_TRANSPORT=mock -e AUTH_DISABLED=1 \
  outlook-mailer:dev
curl http://localhost:8765/healthz   # {"ok":true}
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff and pytest on every pull request and push. Pushes to `main`
and `v*` tags then build the image and push it to `ghcr.io/jakewelliott/outlook-mailer` (see
[Updating](#updating) for the tags). The package must stay public in GHCR (GitHub > Packages > outlook-mailer >
Package settings) so Unraid can pull without logging in.

## Credits and licenses

- Email template adapted from [Cerberus](https://github.com/TedGoas/Cerberus) by Ted Goas, MIT license
  (`app/templates/email/LICENSE-cerberus.txt`).
- Email images: public domain or drawn for this project; sources and licences in
  `app/static/email-img/CREDITS.md`.
- Python libraries as listed in `pyproject.toml`, each under its own license.
