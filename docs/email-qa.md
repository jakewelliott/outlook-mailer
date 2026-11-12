# Email rendering checklist (manual)

Automated tests check the HTML (no scripts, image attributes, size, footer, escaping), but only real mail apps show
how it looks. Use this list once test sends through Graph work. `docs/sample-email.html` is the same template with
a sample body; open it in a browser first for a quick look.

## First real send (do this once, before the first bulk send)

Nothing in the automated tests talks to Microsoft. This is the first time the app really sends, so go step by step
and write the results in the table at the end.

1. **Configure.** In the container settings: `MAIL_TRANSPORT=graph`, `MS_CLIENT_ID` (from
   [entra-setup.md](entra-setup.md)), ideally `TOKEN_CACHE_KEY`; restart. The menu shows **not connected**.
2. **Connect the account.** Account page → **Connect Outlook account** → open the link shown, enter the code, sign
   in with the @outlook.com account, **Accept**. The page shows *Sending as you@outlook.com*. The home page says
   *Connected as ...*.
3. **Make a test draft.** New send → upload a small CSV with **only your own addresses** (an Outlook.com one and a
   Gmail one, e.g. `Email,First Name` / `you@outlook.com,Jake` / `you@gmail.com,Jake`). Map the columns, continue
   to compose, write the "uses everything" message from *Before you start* below, fill in the footer, **Save**.
   The Send section says *Send a test of this version first*, and the start page has no start button.
4. **Send test to me.** In *Send a test to yourself*, pick row 1 and select **Send test to me**. Expected: *Test sent
   to you@outlook.com at ...* and the status line *Test sent to ... for this version*. If it says *The test wasn't
   sent: ...*, note the message and code (e.g. `HTTP 403 ErrorAccessDenied`) and see Troubleshooting in
   entra-setup.md. Nothing else is needed for a retry except waiting 30 seconds.
5. **Check it arrived.** In Outlook.com: the test is in the Inbox (or Junk: note it) with subject
   `[TEST] ...`, and a copy is in **Sent Items**.
6. **Check the headers** (Outlook.com: open the message → ⋯ → View → **View message source**; Gmail: ⋮ → **Show
   original**). Record in the table:
   - `From:` is your @outlook.com address. Which display name is shown: the business name from the footer, or your
     Microsoft profile name?
   - `To:` is only your address; there is no `Cc`/`Bcc`.
   - `List-Unsubscribe: <https://<your domain>/u/.../one-click>` is present, on one line, and the link is the
     Unsubscribe link at the bottom of the email with `/one-click` added (the header is for mail apps' one-click
     button; the footer link opens the confirmation page).
   - `List-Unsubscribe-Post: List-Unsubscribe=One-Click` is present.
   - `Message-ID:` ends in `@<your domain>` (kept) or was replaced by Microsoft (write down what it is).
   - `Content-Type: multipart/alternative` with a `text/plain` and a `text/html` part.
   - Gmail only: *Show original* says SPF, DKIM and DMARC **PASS**.
7. **Rendering.** Go through *Mail apps* and *What to look for* below with the test in Outlook.com (and any other app
   that can open that mailbox, e.g. the Outlook phone app).
8. **Tiny real send.** The test only ever goes to the signed-in Outlook address (forwarding it would change the
   headers), so the Gmail copy comes from a real send of the two-address list: select **Start sending** and send it
   (2 emails). Check the Gmail copy: headers as in step 6, rendering in Gmail web and the Gmail app, and whether
   Gmail shows its own *Unsubscribe* next to the sender name (Gmail only does that for some senders, sometimes
   only after a few emails, so "not yet" is a normal answer). Clicking the footer **Unsubscribe** link opens the
   confirmation page; it records nothing until you type the address and press the button (if you do, that
   address is in `unsubscribed.txt` from then on).
9. **Edit check** (on a new upload of the list, since each upload is sent once): change one word and save; the
   compose page says *The message changed since your last test* and the start page refuses until a new test is
   sent.

### Results of the first real send

Fill in, with the date. Anything marked "no" is worth telling the developer about.

| Check | Outlook.com copy | Gmail copy | Notes |
|---|---|---|---|
| Date tested | | | |
| Test send accepted (no error on the page) | | n/a | |
| Landed in Inbox (not Junk/Spam) | | | |
| Copy in Sent Items | | n/a | |
| `From` address = your @outlook.com | | | |
| `From` display name shown (business name or profile name?) | | | |
| Only one `To`, no `Cc`/`Bcc` | | | |
| `List-Unsubscribe` present and correct | | | |
| `List-Unsubscribe-Post: List-Unsubscribe=One-Click` present | | | |
| `Message-ID` kept (`@your domain`) or replaced (write it) | | | |
| Plain-text and HTML parts both present | | | |
| SPF / DKIM / DMARC (Gmail "Show original") | n/a | | |
| Mail app shows its own Unsubscribe button | | | |
| Rendering checklist below passed | | | |

## Before you start

- [ ] The app is reachable at `PUBLIC_BASE_URL` and `https://<your domain>/img/holly-v1.png` opens **in a private
      window without signing in** (the Cloudflare Access Bypass policy for `/img/*` works). If it asks you to sign
      in, every recipient will see broken images.
- [ ] Write a test message that uses everything: a `#` heading, a `##` heading, **bold**, *italic*, ~~strike~~,
      ++underline++, a link, a bullet list, a numbered list, a long paragraph, `{{first_name|Friend}}`, and a
      two-line sign-off.
- [ ] Fill in the footer: business name, a two- or three-line postal address, advertisement notice on.
- [ ] Check the test (it goes to your Outlook.com address) and the Gmail copy from the tiny real send (step 8
      above) in the apps below.

## Mail apps

Tick each box that passes. For each app, check light mode, dark mode and (where it has the setting) images off.

| App | Light | Dark | Images off | Notes |
|---|---|---|---|---|
| Outlook.com (web) | [ ] | [ ] | [ ] | Settings → General → Appearance → Dark mode; images off: Settings → Mail → Junk email / "Block attachments, pictures and links from outside sources" or open from the Junk folder |
| Outlook desktop, classic (Windows) | [ ] | [ ] | [ ] | If you have it. File → Office Account → Office Theme "Black" for dark. Images off: Trust Center → Automatic Download |
| Outlook desktop, new (Windows/Mac) | [ ] | [ ] | | If you have it |
| Outlook app (iOS/Android) | [ ] | [ ] | | |
| Gmail (web) | [ ] | n/a | [ ] | Images off: Settings → General → Images → "Ask before displaying external images" |
| Gmail app (iOS/Android) | [ ] | [ ] | | Gmail's app inverts colours in dark mode; check text is still readable |
| Apple Mail / iOS Mail | [ ] | [ ] | [ ] | Images off: Settings → Mail → "Load Remote Images" off (iOS); "Protect Mail Activity" may preload images |

## What to look for

Header and layout
- [ ] Header band: the Christmas-tree photo on the right, the business name in gold and "Merry Christmas" in
      large cream text on the dark left side. The text must be readable over the photo.
- [ ] With images off, the header is a solid dark green band and the text is still there and readable.
- [ ] Classic Outlook: the header photo shows behind the text (VML background), the layout is exactly 600px wide
      and centred, no extra gaps between sections.
- [ ] Gold lines above and below the message card; the card is cream on a slightly darker cream page.
- [ ] Holly divider at the top of the card: sharp (not blurry) and about 120×50. With images off it leaves
      only a small gap (it has empty alt text on purpose, it's decoration).
- [ ] On a phone the email fits the screen width, nothing scrolls sideways, the headline wraps neatly, and text
      is at least 16px.

Body
- [ ] Headings are dark red in a serif font (Georgia), body text dark brown, links green and underlined.
- [ ] Bold, italic, strike and underline all show. Lists are indented with bullets/numbers.
- [ ] The merge field shows the recipient's first name (or the fallback).
- [ ] Line breaks you typed with Enter are kept.

Dark mode
- [ ] Page and card turn dark, text turns light, headings light red, links light green.
- [ ] Nothing is dark-on-dark or light-on-light (check the footer and the header especially).
- [ ] The holly keeps its light outline and is visible on the dark card.

Footer (CAN-SPAM)
- [ ] Business name, full postal address (each line on its own line), "This message is an advertisement.",
      the "why you're receiving this" line, and the Unsubscribe link are all visible and readable.
- [ ] Phone/address text is not turned into blue auto-links (iOS).
- [ ] Clicking **Unsubscribe** opens the confirmation page on `PUBLIC_BASE_URL/u/...` (no sign-in); it does not
      unsubscribe you until you type the address the email was sent to and press the button.
- [ ] The mail app's own "Unsubscribe" button (Gmail, Outlook.com, Apple Mail) appears once the
      List-Unsubscribe headers are present (check step 6). It may take a few sends from the account before Gmail
      shows it.

Inbox and technical
- [ ] The inbox preview shows the preheader text after the subject, not "The Calico Barn Merry Christmas…".
- [ ] Gmail does not show "[Message clipped] View entire message" at the bottom (the HTML stays under 102 KB).
- [ ] "Show original" / "View message source" has both a plain-text and an HTML part; the plain-text part ends
      with the same footer and the full unsubscribe address.
- [ ] The test email landed in the inbox, not spam. If it went to spam, note which app and whether images or
      links were blocked.

Write down anything that looks wrong with the app name, light/dark, and a screenshot.
