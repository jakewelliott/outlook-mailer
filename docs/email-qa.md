# Email rendering checklist (manual)

Automated tests check the HTML (no scripts, image attributes, size, footer, escaping), but only real mail apps show
how it looks. Use this list in stage 12, once test sends through Graph work. `docs/sample-email.html` is the same
template with a sample body; open it in a browser first for a quick look.

## Before you start

- [ ] The app is reachable at `PUBLIC_BASE_URL` and `https://<your domain>/img/holly-v1.png` opens **in a private
      window without signing in** (the Cloudflare Access Bypass policy for `/img/*` works). If it asks you to sign
      in, every recipient will see broken images.
- [ ] Write a test message that uses everything: a `#` heading, a `##` heading, **bold**, *italic*, ~~strike~~,
      ++underline++, a link, a bullet list, a numbered list, a long paragraph, `{{first_name|Friend}}`, and a
      two-line sign-off.
- [ ] Fill in the footer: business name, a two- or three-line postal address, advertisement notice on.
- [ ] Send the test to the accounts below (an Outlook.com and a Gmail address at least).

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
- [ ] Clicking **Unsubscribe** opens the confirmation page (stage 13) on `PUBLIC_BASE_URL/u/...`; it does not
      unsubscribe you until you press the button.
- [ ] The mail app's own "Unsubscribe" button (Gmail, Outlook.com, Apple Mail) appears once the
      List-Unsubscribe headers are in place (stage 9/12). It may take a few sends from the account before Gmail
      shows it.

Inbox and technical
- [ ] The inbox preview shows the preheader text after the subject, not "The Calico Barn Merry Christmas…".
- [ ] Gmail does not show "[Message clipped] View entire message" at the bottom (the HTML stays under 102 KB).
- [ ] "Show original" / "View message source" has both a plain-text and an HTML part; the plain-text part ends
      with the same footer and the full unsubscribe address.
- [ ] The test email landed in the inbox, not spam. If it went to spam, note which app and whether images or
      links were blocked.

Write down anything that looks wrong with the app name, light/dark, and a screenshot.
