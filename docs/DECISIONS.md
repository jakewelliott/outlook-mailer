# Decisions and why

Agreed with Jake between 2026-10-25 and 2026-11-11. The README summarises these; this file has the reasoning.

| Topic | Decision | Why |
|---|---|---|
| Sending account | Free personal @outlook.com. Nothing paid. | Jake's requirement. |
| Sending method | Microsoft Graph `sendMail`, delegated OAuth. | Microsoft switched off password/app-password SMTP for Outlook.com in Sept 2024. OAuth is the only way left. |
| App registration | Lives in the Entra tenant that comes with a free Azure account. | Personal accounts can no longer register apps outside a directory. The free Azure account is $0 and permanent; the card is only for identity checks. |
| Sign-in | One-time device code from the web UI (enter a code at microsoft.com/link). Token stored in `/data`, encrypted if `TOKEN_CACHE_KEY` is set. | The Outlook password can't be used by apps. The token renews itself; re-sign-in only after a password change or ~90 days unused. |
| Message format | MIME: HTML with a plain-text alternative. | Graph's JSON send allows one body only; MIME keeps the text version and lets us add List-Unsubscribe headers. |
| Sent Items | Copies saved to Outlook's Sent folder. | A record of what went out. |
| Pacing | 6 per minute, 250 per day by default (env-configurable). | Microsoft doesn't publish free-account limits; ~300/day is reported. Staying under it avoids the account being flagged or locked. |
| Email type | Commercial. Footer has the advertisement notice (default on), postal address and unsubscribe link. | CAN-SPAM requirements for commercial email. |
| Unsubscribe | One-click link only (no reply-to-unsubscribe). Opt-outs appended to `/data/unsubscribed.txt` as `email<TAB>datetime`. | Jake removes them from his CSV himself. The link must keep working 30+ days after a send (CAN-SPAM). |
| Send-time safety net | Every recipient is checked against `unsubscribed.txt` before sending. | If an opt-out is missed in the CSV it still isn't emailed. CAN-SPAM requires honouring opt-outs within 10 business days. |
| Subscription status | Optional dropdown. If set, the user picks which values to send to. If blank, everyone is eligible. | Square exports include "Email Subscription Status"; other exports may not. |
| Column mapping | Two required dropdowns (email, first name) from the CSV header. | Works with any export without caring about its format. |
| Body editor | Markdown: bold, italic, strike, `++underline++`, links, lists. | Simple formatting without a rich-text editor. Markdown has no underline, so `++text++` was chosen. |
| Template | Based on Cerberus (MIT), Christmas styled. | Proven to render in classic Outlook, Gmail, Apple Mail, dark mode. |
| Images | Hosted by the app at `/img/*`; all free-license, credited in `app/static/img/CREDITS.md`. | Hosted images look better than none; the server is already public for unsubscribe. Container must stay up 30+ days after sending. |
| Access | Cloudflare tunnel + Cloudflare Access app; `/u/*`, `/img/*`, `/healthz` bypassed. App verifies the Access JWT on admin pages. No separate app login. | Cloudflare decides who can use the app. The JWT check stops anyone reaching the admin pages via the LAN port. |
| Domain | `PUBLIC_BASE_URL` env var (Jake uses `https://the-calico-barn.jakewelliott.com`). | Easy to change. |
| Port | 8765. | Free on the server. |
| Repo / image | Public repo `jakewelliott/outlook-mailer`; public image `ghcr.io/jakewelliott/outlook-mailer`. | No secrets in either. A public image means Unraid can pull without logging in (`/root` is wiped on reboot). |
| CSV retention | Raw CSV deleted after the campaign; send log kept. | The CSV changes every time. |
| Stack | Python 3.12, FastAPI, Jinja2, SQLite. | Small, no frontend build, first-class Microsoft auth library (MSAL). |
