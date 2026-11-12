# outlook-mailer: build plan and specification

This is the spec every stage is built and reviewed against. Decisions and the reasons for them are in
[DECISIONS.md](DECISIONS.md). Background research is in [RESEARCH.md](RESEARCH.md).

## What it is

A single Docker container with a web UI. The user uploads a CSV, maps columns, writes a Markdown email, fills in the
CAN-SPAM footer, previews it, sends a test to themselves, then runs a rate-limited bulk send. Mail goes out from a
**free personal @outlook.com account** through Microsoft Graph, one email per recipient, using a Christmas HTML
template that works in all major email clients. GitHub Actions builds the image and pushes it to
`ghcr.io/jakewelliott/outlook-mailer` (public). The server only pulls the image.

## Stack

- Python 3.12, FastAPI, Jinja2 (server-rendered pages), a little vanilla JS. No frontend build.
- Libraries: `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `msal`, `httpx`, `email-validator`, `css-inline`,
  `charset-normalizer`, `markdown-it-py`, `pyjwt[crypto]`, `cryptography`. Dev: `pytest`, `respx`, `ruff`.
- State in `/data` (Unraid: `/mnt/user/appdata/outlook-mailer`), owned by uid 99 / gid 100:
  - `app.db`: SQLite (stdlib). Campaigns, recipients, settings.
  - `msal_cache.json`: token cache, mode 0600, encrypted with `TOKEN_CACHE_KEY` if set.
  - `unsubscribed.txt`: one line per opt-out, `email<TAB>ISO-8601 datetime with offset`. Append-only by the app.
  - `outbox/`: `.eml` files when `MAIL_TRANSPORT=mock`.
- One uvicorn worker (the send worker runs in-process).

## Environment variables

| Name | Required | Default | Purpose |
|---|---|---|---|
| `PUBLIC_BASE_URL` | yes | none | Public https address, e.g. `https://the-calico-barn.jakewelliott.com`. Used for unsubscribe links and image URLs in emails. No trailing slash. |
| `UNSUBSCRIBE_SECRET` | yes | none | Random string, 32+ chars. HMAC key for unsubscribe tokens. Changing it breaks links in already-sent emails. |
| `MS_CLIENT_ID` | yes for real sending | none | Entra app (client) ID. Public client, no secret. |
| `MS_AUTHORITY` | no | `https://login.microsoftonline.com/consumers` | Leave as-is for @outlook.com. |
| `CF_ACCESS_TEAM_DOMAIN` | yes unless `AUTH_DISABLED=1` | none | e.g. `jakewelliott.cloudflareaccess.com` |
| `CF_ACCESS_AUD` | yes unless `AUTH_DISABLED=1` | none | Application Audience (AUD) tag of the Cloudflare Access app. |
| `AUTH_DISABLED` | no | `0` | `1` turns off the Access check on admin pages. Local testing only. Logs a loud warning. |
| `MAIL_TRANSPORT` | no | `graph` | `graph` or `mock`. |
| `SEND_RATE_PER_MINUTE` | no | `6` | Pacing between messages. |
| `DAILY_SEND_CAP` | no | `250` | Rolling 24 h cap. The worker pauses and resumes on its own. |
| `MAX_RECIPIENTS` | no | `2000` | Upload row limit. |
| `MAX_UPLOAD_MB` | no | `5` | CSV size limit. |
| `TOKEN_CACHE_KEY` | no | none | Fernet key to encrypt the token cache. |
| `DATA_DIR` | no | `/data` | |
| `PORT` | no | `8765` | |
| `TZ` | no | `America/New_York` | |
| `LOG_LEVEL` | no | `INFO` | |
| `FORWARDED_ALLOW_IPS` | no | `*` | Comma-separated IPs/CIDRs of the proxy (cloudflared) trusted for `X-Forwarded-For` / `X-Forwarded-Proto`. Tightening it to cloudflared's address or Docker subnet is recommended. Read by uvicorn (the container CMD), not by `config.py`. |

The app refuses to start if a required variable is missing or invalid, and says which one.

## Access model

- The UI is reached through the Cloudflare tunnel at `PUBLIC_BASE_URL`, protected by a Cloudflare Access application.
- **Public paths** (Cloudflare Access must have a Bypass policy for these; the app does not check auth on them):
  `/u/*` (unsubscribe), `/img/*` (email images), `/healthz`.
- **Everything else is admin.** The app verifies the `Cf-Access-Jwt-Assertion` header on every admin request
  (RS256, keys from `https://<CF_ACCESS_TEAM_DOMAIN>/cdn-cgi/access/certs`, cached, `aud` = `CF_ACCESS_AUD`,
  `iss` = `https://<CF_ACCESS_TEAM_DOMAIN>`, `exp` checked). Missing or invalid gives 403. This also blocks anyone
  hitting the LAN port directly. No separate app login.
- CSRF token on all admin POSTs, plus an Origin check. CSP `default-src 'self'`; the email preview is served from its own admin route with a narrow CSP (inline styles and email images only) and shown in an `<iframe sandbox>`.
  Admin responses send `Referrer-Policy: same-origin` (public paths `no-referrer`); `Origin: null` is rejected.

## CSV and mapping

- Upload any CSV. The app detects encoding (UTF-8, UTF-8 BOM, cp1252) and delimiter (`,` `;` tab). First row is the header.
- Three dropdowns, filled from the header row:
  - **Email address** (required)
  - **First name** (required)
  - **Email subscription status** (optional, may be left blank)
- If a status column is chosen, the UI lists its distinct values with counts and a checkbox each: "send to rows with
  this value". Values that look like opt-outs start unchecked; others start checked. Matching is on whole words: a
  word starting with `unsub`, `notsub`, `nonsub` or `bounc`; "opt(ed) out" in any spelling; a negative word (`no`,
  `not`, `false`, `0`, `never`, `declined`…); or `pending`, `unconfirmed`, `cleaned`, `unknown`. `opt` alone is not a
  signal (`opted in` stays checked) and `not` inside a word doesn't count (`Notified` stays checked). Blank starts
  unchecked and is labelled "(blank — no status)"; ticking it sends to rows that have an email but no status. Square's
  real values are `subscribed`, `unsubscribed`, blank and `bounced`. Unchecked rows are skipped and reported as
  `skipped_status`.
- If no status column is chosen, everyone on the list is eligible.
- Validation report: invalid email, duplicate email (keep first), missing email, unsubscribed (in `unsubscribed.txt`),
  skipped by status. Counts plus a downloadable list.
- Other columns are ignored. The raw CSV is deleted when the campaign finishes or is cancelled; the send log is kept.

## Merge fields

- `{{first_name}}`, `{{email}}`, with a fallback: `{{first_name|Friend}}`.
- Custom regex engine. **Never evaluate user text as Jinja.** Unknown fields block sending.
- HTML-escaped in the HTML part, raw in the text part, CR/LF and control characters stripped in the subject.
- Usable in subject, body and preheader.

## Body: Markdown

- `**bold**`, `*italic*`, `~~strike~~`, `++underline++`, `[text](https://url)`, blank-line paragraphs, line breaks,
  bullet and numbered lists, headings `#`/`##`.
- Raw HTML in the Markdown is not allowed (escaped). Links only `http`, `https`, `mailto`.
- Output is email-safe HTML (inline styles applied by the template/inliner), plus a plain-text rendering
  (bold/italic markers removed, links as `text (url)`).

### Merge fields in the Markdown body (stage 6 must handle these)

Stage 5 (`app/merge.py`) leaves three hazards for stage 6. Each needs a test.

- **Validate the raw body first.** Run `merge.validate()` on the body exactly as Jake typed it, before
  `protect()` and Markdown, so error messages quote his text. The re-validation inside `render()` after Markdown
  is only a backstop.
- **A placeholder in a link target gets past the scheme check.** `[x]({{first_name}})` converts to
  `href="{{first_name}}"`, which passes an http/https/mailto check as a relative URL. The recipient's value then
  becomes the whole href (`javascript:…` or a phishing URL). Rule: a placeholder may appear in a link target only
  after a fixed `https://host/…` or `http://host/…` prefix (scheme and host written out by Jake, before any
  placeholder), or as exactly `mailto:{{email}}`. Anything else is a validation error shown to Jake before sending.
  As a second line of defence, re-check link schemes in the merged HTML per recipient.
- **Values inside URLs must be percent-encoded, not only HTML-escaped.** A value like `a b#c` would otherwise
  break the URL or change its meaning. The merge needs a URL-context render for placeholders inside `href`:
  encode the value with `urllib.parse.quote`, then HTML-escape it for the attribute. Plain-text output shows
  links as `text (url)`, so use the same encoded URL there.

## Email template

- Based on **Cerberus** (MIT license, by Ted Goas): table layout, MSO ghost tables, VML background, dark-mode handling.
  Credit and license kept in `app/templates/email/LICENSE-cerberus.txt`.
- Christmas styling: deep red, pine green and gold on cream; serif headings (Georgia stack).
- Images served by the app at `PUBLIC_BASE_URL/img/...`, absolute URLs in the email. Every image has alt text,
  width and height, and the email reads fine with images off. **All images free-license**, each listed with source,
  author and license in `app/static/email-img/CREDITS.md`. Prefer CC0 / public domain. Formats PNG or JPG (no SVG, no
  data URIs). Keep total image weight small (under about 300 KB).
- Footer (CAN-SPAM, commercial): business/sender name, postal address, "This message is an advertisement" (toggle,
  default on), why you're receiving this, and an **Unsubscribe link** (`PUBLIC_BASE_URL/u/<token>`). No mailto opt-out.
- HTML under 102 KB (Gmail clipping). No `<script>`, no external CSS or web fonts.
- As built (stage 7): `app/render.py` (`render_email`, `render_text`, `FooterInfo`) and
  `app/templates/email/christmas.html.j2`. The header band is a photo used as a CSS/VML background behind live text
  (dark green `bgcolor` with images off); the holly divider is an `<img>` with empty `alt` (decoration). Images live in
  `app/static/email-img/` with a version in the file name and are served at `/img/<file>` with
  `Cache-Control: public, max-age=31536000, immutable`; to change one, add a new file name (sent emails keep
  linking to the old one). Only `.png`/`.jpg`/`.jpeg`/`.gif` are served there. Manual client checklist:
  [email-qa.md](email-qa.md); rendered sample: [sample-email.html](sample-email.html).

## Sending

- MS Graph `POST /v1.0/me/sendMail` with MIME content (base64 `text/plain` body), so the email is
  `multipart/alternative` (text + HTML) and carries `List-Unsubscribe: <https://.../u/<token>>` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. Copies saved to Sent Items. If MIME headers are stripped,
  the footer link still satisfies CAN-SPAM.
- Delegated `Mail.Send` + `User.Read` + `offline_access`, device code sign-in from the UI, authority `/consumers`.
  The UI shows the user code and MSAL's `verification_uri` (normally microsoft.com/link for personal accounts),
  never a hardcoded URL.
- One recipient per message. Never CC/BCC.
- Pacing `SEND_RATE_PER_MINUTE`, rolling `DAILY_SEND_CAP`, retries with backoff honouring `Retry-After` (max 3),
  stop the campaign on auth errors or N consecutive failures. Pause, resume, cancel. Resumes after restart with no
  double sends.
- Bulk send is locked until a test send of the **current** content (hash) to the signed-in address succeeds.
- **At send time** every recipient is checked against `unsubscribed.txt` and skipped if present.
- As built (stage 12): `app/transport/graph.py` (response mapping table in its docstring; DECISIONS "Graph transport"
  and "Graph responses") and `app/gate.py` (test send and gate; DECISIONS "Test send and gate"). The test send
  ("Send test to me" on the compose page) is required in mock mode too, counts toward the daily cap, and is
  limited to 1 per 30 s and 20 per 24 h (refused while a campaign is sending, and paced after a bulk send). A
  graph campaign stops if a different account than the tested one is connected. First real send steps and results:
  [email-qa.md](email-qa.md).

## Unsubscribe

- Token: `base64url(email) . HMAC-SHA256(UNSUBSCRIBE_SECRET, email)` truncated. Verified with constant-time compare.
- `GET /u/<token>`: a small confirmation page with one "Unsubscribe" button (so link scanners don't unsubscribe
  people). `POST /u/<token>` (and the RFC 8058 one-click POST) records the opt-out and shows "You're unsubscribed".
- Recording: append `email<TAB>timestamp` to `unsubscribed.txt` (skip if already present), with a file lock.
- Rate-limited, no auth, no other data revealed. Must keep working at least 30 days after a send.
- The rate limiter keys on the `CF-Connecting-IP` header (Cloudflare sets it and overwrites client values), falling
  back to the socket peer address when it is absent. Never on `request.client.host` or `X-Forwarded-For`: with
  `--proxy-headers` uvicorn takes the leftmost `X-Forwarded-For` entry, which the client controls. Only someone
  hitting the LAN port directly could spoof `CF-Connecting-IP`, which is acceptable. Logs may show spoofed IPs.
- Admin page shows the list and offers a download of the file.

## Stages

Each stage: one coding agent, one reviewer, one commit to `main`. Tests run in Docker (the host has no Python):
`docker run --rm -v "$PWD":/src -w /src python:3.12-slim sh -c "pip install -q -e '.[dev]' && ruff check . && pytest -q"`.

| # | Stage | Acceptance highlights |
|---|---|---|
| 1 | Scaffold: pyproject, `app/main.py`, `app/config.py`, `/healthz`, tests, `.gitignore`, README skeleton | `/healthz` → `{"ok":true}`; config validation per the env table; ruff + pytest green |
| 2 | Dockerfile, `.dockerignore`, `compose.yaml`, GitHub Actions CI to GHCR | image < ~250 MB, runs as 99:100, healthcheck healthy, `/data` writable; CI green, `latest` + `sha-` tags pushed |
| 3 | UI shell, Cloudflare Access JWT check, CSRF, security headers, help page | 403 without valid JWT on admin; public paths open; help page lists steps and merge fields |
| 4 | CSV upload and the three mapping dropdowns, status value picker, validation report | encodings/delimiters fixtures; required dropdowns enforced; status filter works |
| 5 | Merge engine | fallbacks, escaping, unknown-field errors, `{{7*7}}` not evaluated |
| 6 | Markdown → email HTML + plain text | all syntax above incl. `++underline++`; raw HTML escaped; unsafe links rejected; merge-field hazards in "Merge fields in the Markdown body" handled (raw body validated first, placeholders only after a fixed `http(s)://host/` prefix or as `mailto:{{email}}`, values in URLs percent-encoded) |
| 7 | Christmas template (Cerberus-based) + free-license images at `/img/*` + CREDITS.md | lint checks (no script, alt/width/height, < 102 KB, absolute image URLs); snapshot tests |
| 8 | Compose page, saved settings, live preview (light/dark) | footer required fields enforced; preview for any row. Note: an `<iframe sandbox srcdoc>` inherits the admin CSP (`style-src 'self'`), so the preview needs a per-route CSP allowing inline styles for that frame, or is served from its own route with its own CSP |
| 9 | Message builder (MIME) + transport interface + mock transport | valid multipart/alternative `.eml`; single recipient; List-Unsubscribe headers |
| 10 | Send job engine, progress UI, results CSV, CSV deletion | rate/cap/retry/pause/resume/cancel; exactly-once across restart; `unsubscribed.txt` checked at send time |
| 11 | Microsoft device-code sign-in, encrypted token cache, account page | mocked MSAL tests; manual sign-in (needs Jake) |
| 12 | Graph transport, test-send gate | respx tests; manual real test send and header check (needs Jake). Map responses to `SendResult` as documented in `app/transport/base.py` (retryable only when certainly not accepted; otherwise unknown); request timeout below `SHUTDOWN_GRACE_SECONDS` (15 s) |
| 13 | Unsubscribe pages, `unsubscribed.txt`, admin view/download | token tests; GET doesn't unsubscribe; POST appends once. Note: `/static/` is admin-only, so public pages need their own minimal styling and CSP |
| 14 | README (decisions + fresh-clone setup), `docs/entra-setup.md`, `docs/cloudflare.md`, `unraid/outlook-mailer.xml`, v1.0.0 | someone can set up from a fresh clone using only the README; the Unraid template sets `--stop-timeout=30` in Extra Parameters (see DECISIONS "Shutdown timing") |

Stages 1–10 and 13 need no Microsoft account (`MAIL_TRANSPORT=mock`, `AUTH_DISABLED=1` in tests).
