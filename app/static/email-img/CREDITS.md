# Email image credits

Images in this folder are served publicly at `PUBLIC_BASE_URL/img/<file>` and linked from sent emails.
All are public domain or the project's own work. Licences checked on the source pages on 2026-11-11.

Never replace a file in place: emails already sent link to it and it is cached as immutable for a year.
Add a new file with a new version suffix (`-v2`) and change `IMAGES` in `app/render.py`.

## christmas-banner-v1.jpg

Background of the header band (shown at 600×220; the text on it is live HTML text).

- **Title:** Christmas decorations 2017 G1 ("Decorations on the Christmas tree. Ukraine.")
- **Author:** George Chernilevsky
- **Source:** https://commons.wikimedia.org/wiki/File:Christmas_decorations_2017_G1.jpg
- **Licence:** Public domain, released by the author (`{{PD-self}}`: "I, the copyright holder of this work, release
  this work into the public domain. This applies worldwide."),
  https://commons.wikimedia.org/wiki/Template:PD-self
- **Changes:** cropped to a 3600×1320 strip around the red bauble (from x=400, y=760 of the 4000×4000
  original), scaled to 1200×440, dark pine-green overlay (`#0e261a`, 88% opacity) over the left 42% fading out
  by 62% so the live text is readable, saved as progressive JPEG (quality 78), metadata removed.

## holly-v1.png

Small divider at the top of the message card (shown at 120×50).

- **Title:** Holly sprig
- **Author:** drawn for this project with Pillow (two-tone holly leaves, three berries, cream outline so it
  stays visible on dark backgrounds)
- **Source:** own work, this repository
- **Licence:** CC0 1.0 Universal (public domain dedication), https://creativecommons.org/publicdomain/zero/1.0/
- **Changes:** none (created at 240×100, transparent PNG)
