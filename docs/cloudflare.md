# Cloudflare setup: tunnel, Access and security settings

outlook-mailer is reached only through a Cloudflare Tunnel, and Cloudflare Access decides who may use the admin
pages. This guide sets up both for a domain that is already on Cloudflare. The example address is
`https://the-calico-barn.jakewelliott.com` (`PUBLIC_BASE_URL`); use your own.

Steps and labels were checked against Cloudflare's documentation on 2026-11-12. Cloudflare moves dashboard menus
around often; if a label differs, look for the closest match. Labels that could not be confirmed in Cloudflare's
docs are marked **(not verified)**.

## What you'll end up with

- A tunnel route (published application) from the public hostname to the container on port 8765.
- An Access application for the hostname that only lets you in, plus a **Bypass** for the three public paths:
  `/u/*` (unsubscribe pages and the one-click POST), `/img/*` (images in sent emails) and `/healthz`.
- Two values for the container: `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD`.
- No Cloudflare challenges on `/u/*`, and `CF-Connecting-IP` passed to the app.

Why it matters: every email links to `/u/...` and `/img/...`. If Access or a challenge page sits in front of them,
recipients can't unsubscribe (a CAN-SPAM problem, and mail providers' one-click requests fail without anyone
noticing) and images are broken.

## 1. Tunnel route

This assumes a `cloudflared` tunnel is already running (on Jake's server it is its own container). Create the
**Access application (step 2) first** if you can: Cloudflare recommends that order, so the hostname is never
public without Access in front of it.

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/), go to **Networking** > **Tunnels** and select the
   tunnel. (Older dashboards: **Zero Trust** > **Networks** > **Tunnels**.)
2. On the **Routes** tab, select **Add route**, then **Published application**. (Older dashboards call this a
   **Public hostname**.)
3. **Subdomain** `the-calico-barn`, **Domain** `jakewelliott.com`, **Path** empty.
4. **Service URL**: `http://<server-LAN-IP>:8765`, e.g. `http://192.168.1.10:8765`.
   - Use the server's LAN IP, **not** `localhost`: the tunnel container is on a Docker bridge network, where
     `localhost` is the tunnel container itself.
   - `http`, not `https`: the app speaks plain HTTP; Cloudflare provides HTTPS to visitors.
   - If you change the published port (or `PORT`), change it here too.
5. Leave the origin option **Protect with Access** (sometimes shown under the route's additional settings) **off**.
   It makes `cloudflared` demand an Access token on every request, including the bypassed `/u/*` and `/img/*`,
   which have none. The app checks the Access token itself on admin pages.
6. Save. Cloudflare creates the DNS record (a proxied `CNAME` to the tunnel) for you.

Give the server a fixed LAN IP (a DHCP reservation in the router); if it changes, the tunnel stops reaching the
app.

## 2. Access application

### Team domain (`CF_ACCESS_TEAM_DOMAIN`)

In the dashboard go to **Zero Trust** > **Settings** and find **Team name and domain**. The team domain is
`<team-name>.cloudflareaccess.com`. Put the **bare hostname** in `CF_ACCESS_TEAM_DOMAIN`, e.g.
`jakewelliott.cloudflareaccess.com`: no `https://`, no path (the app refuses to start otherwise). If you have
never opened Zero Trust before, Cloudflare asks you to pick a team name and a plan first; the **Free** plan is
enough.

### Login method

Under **Zero Trust** > **Integrations** > **Identity providers**, **One-time PIN** (a code emailed to you) works
without any setup and is enough here. You can add Google or GitHub instead.

### The application for the whole site

1. Go to **Zero Trust** > **Access controls** > **Applications** (older dashboards: **Access** > **Applications**)
   and select **Create new application** (or **Add an application**).
2. Select **Self-hosted and private** (older dashboards: **Self-hosted**), then **Add public hostname**.
3. **Subdomain** `the-calico-barn`, **Domain** `jakewelliott.com`, **Path** empty. An empty path covers the
   hostname and every path under it.
4. Under **Access policies**, create a policy:
   - **Policy name**: `Jake`
   - **Action**: **Allow**
   - **Include**: selector **Emails**, value your own email address (the one you sign in with).
   Applications deny everyone who doesn't match an Allow policy.
5. **Session duration**: anything you like (24 hours is fine).
6. Select **Create** (or **Save**).

### AUD tag (`CF_ACCESS_AUD`)

Open the application again (**Edit**) and, under **Additional settings** (in some versions the **Overview** tab),
copy the **Application Audience (AUD) Tag**, a 64-character hex string. That is `CF_ACCESS_AUD`. It never changes
unless the application is deleted and recreated; if that happens, update the container.

Use the AUD tag of **this** application (the whole-site one), not of the bypass application below: the app checks
that the token was issued for this application.

### Bypass for the public paths

Cloudflare's documented way to make one path public is a **separate Access application for that path** with a
**Bypass** policy (Cloudflare docs: *Common policies* > *Bypass a public endpoint*); for those paths it takes
the place of the whole-site application, because its path is more specific. The checks in step 6 confirm it.

1. **Create new application** > **Self-hosted and private** > **Add public hostname** again.
2. Name it e.g. `outlook-mailer public paths`.
3. Add three destinations (select **Add public hostname** for each), all with **Subdomain** `the-calico-barn` and
   **Domain** `jakewelliott.com`:
   - **Path** `u/*`
   - **Path** `img/*`
   - **Path** `healthz`

   The dashboard shows the `/` between domain and path itself; type the path without a leading slash if the field
   already shows one **(not verified:** whether the field wants the leading `/` varies between dashboard
   versions). If the dashboard only allows one destination per application, create three applications instead.
4. Policy: **Action** **Bypass**, **Include** selector **Everyone**.
5. Save.

**Sub-paths must be covered.** Mail providers send the one-click unsubscribe as a `POST` to
`/u/<token>/one-click`, one level deeper than the footer link `/u/<token>`. A path wildcard spans `/`
(Cloudflare's examples show `example.com/foo*/bar` matching `example.com/food/stuff/bar`), so `u/*` should cover
both. Cloudflare's page doesn't show a trailing `*` matching several levels, so **check it** with the second
`curl` command below. If that POST gets an Access login page or a redirect instead of the app's 404, add a fourth
destination with **Path** `u/*/one-click` to the bypass application.

A wildcard path doesn't cover its parent: `u/*` doesn't cover `/u` itself, which is fine (the app has nothing
there).

## 3. Security settings that must not block `/u/*`

Gmail, Outlook.com and Apple's servers send the RFC 8058 one-click `POST` without a browser. They can't solve a
challenge, and a challenged request is an opt-out lost without anyone noticing. People clicking the footer link
can usually solve a challenge, but it's better if they never see one.

Check these in the zone (select the domain in the dashboard):

| Setting | Where | What to do |
|---|---|---|
| **Bot Fight Mode** (Free plan) | **Security** > **Settings**, filter **Bot traffic** | **Turn it off.** It runs outside the rules engine, so no rule can exempt a path from it (Cloudflare docs: *Bot Fight Mode* > *Limitations*). |
| **Super Bot Fight Mode** (Pro and above) | same page | If on, add the skip rule below and select **All Super Bot Fight Mode rules**. |
| **I'm Under Attack** mode | **Security** > **Settings** (security level) | Leave it off. If you ever turn it on, first add a configuration rule for `/u/*` with **I'm Under Attack** **Off** (below). |
| **WAF custom rules**, **managed rules**, **rate limiting rules** | **Security** > **Security rules** | Don't add challenges or blocks that match `/u/*`. If you have any, add the skip rule below, placed **first**. |
| **Browser Integrity Check** | **Security** > **Settings** | On by default; it challenges clients with a missing or unusual `User-Agent`. Turn it off for `/u/*` with the configuration rule below. |
| **Remove visitor IP headers** (managed transform) | **Rules** > **Settings** (or **Rules** > **Transform Rules** > **Managed Transforms**) | **Leave it off.** It removes `CF-Connecting-IP`, which the app's unsubscribe rate limiter needs (see below). |

### Skip rule (WAF custom rule)

**Security** > **Security rules** > **Create rule** > **Custom rules**:

- **Rule name**: `outlook-mailer: no challenges on unsubscribe`
- **When incoming requests match**: select **Edit expression** and enter

  ```txt
  (http.host eq "the-calico-barn.jakewelliott.com" and starts_with(http.request.uri.path, "/u/"))
  ```

- **Then take action**: **Skip**, and tick **All remaining custom rules**, **All rate limiting rules**,
  **All managed rules** and (if you have it) **All Super Bot Fight Mode rules**.
- Place it first in the list, then **Deploy**.

### Configuration rule

**Rules** > **Overview** > **Create rule** > **Configuration Rule**, same expression as above, then add these
settings:

- **Browser Integrity Check**: **Off**
- **I'm Under Attack**: **Off** (in older dashboards: **Security Level** > *Essentially Off*)

**Deploy**.

## 4. `CF-Connecting-IP` must reach the app

The unsubscribe rate limiter tells visitors apart by the `CF-Connecting-IP` header, which Cloudflare sets (and
overwrites if a client sends one). Behind `cloudflared` the connection always comes from the tunnel, so without
the header every visitor shares one limit, and one busy visitor or scanner could stop others from opening the
page (valid opt-outs are still recorded either way). Cloudflare sends the header by default; the only thing that
removes it is the **Remove visitor IP headers** managed transform, so leave that off. The app logs this warning
(at most once an hour) when an unsubscribe request arrives without it:

```txt
An unsubscribe request arrived without the CF-Connecting-IP header, ...
```

Requests to the LAN port directly never have the header, so the warning after a LAN test is expected.

## 5. Other zone settings

- **Email Address Obfuscation** (**Scrape Shield**) rewrites email addresses in HTML pages and adds a script to
  show them again. The admin pages list addresses (sends, results, unsubscribes); nothing here needs obfuscation,
  and it hasn't been tested with the app's Content-Security-Policy. If addresses show as `[email protected]`, add
  **Email Obfuscation** **Off** to the configuration rule (with the expression `http.host eq
  "the-calico-barn.jakewelliott.com"`) or turn it off for the zone.
- **Rocket Loader** and other features that rewrite pages or inject scripts: leave them off for this hostname.
- **Caching**: nothing to set. `/u/*` pages send `Cache-Control: no-store` and images are versioned and immutable.

## 6. Checks after setup

From any computer **not** signed in to Access (or a private browser window):

```sh
# Public paths: answered by the app, no login
curl -i https://the-calico-barn.jakewelliott.com/healthz               # 200 and {"ok":true}
curl -i https://the-calico-barn.jakewelliott.com/u/x                   # 404, "This link isn't valid"
curl -i -X POST https://the-calico-barn.jakewelliott.com/u/x/one-click # the same 404 page from the app
curl -sI https://the-calico-barn.jakewelliott.com/img/holly-v1.png     # 200, content-type: image/png

# Admin: Access login
curl -i https://the-calico-barn.jakewelliott.com/                      # 302 to <team>.cloudflareaccess.com
```

- The two `/u/x` commands must show the app's own *This link isn't valid* page (HTTP 404). A Cloudflare sign-in
  page, a `302` redirect to `cloudflareaccess.com`, a `403` from Cloudflare, or a challenge page ("Just a
  moment...") means the bypass or the challenge settings are wrong.
- `/` must redirect to the Access login. If it shows the app's page without signing in, the whole-site
  application is missing; if it says *Access denied: This page must be opened through Cloudflare Access* after you
  signed in, `CF_ACCESS_TEAM_DOMAIN` or `CF_ACCESS_AUD` is wrong (or you copied the bypass application's AUD tag).
- In a browser, open `https://the-calico-barn.jakewelliott.com/`, sign in with your email: the app's home page
  (*How it works*) appears.
- From the LAN: `curl -i http://<server-LAN-IP>:8765/` gives `403 Access denied` (the app's own token check, so the
  admin pages can't be used by going round Cloudflare), and `curl http://<server-LAN-IP>:8765/healthz` gives
  `{"ok":true}`.
- After the `/u/x` checks through Cloudflare, `docker logs outlook-mailer` must **not** show the
  `CF-Connecting-IP` warning (the LAN test may).

## References

Cloudflare pages checked on 2026-11-12:

- [Create a remotely-managed tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/) and [Tunnel: get started](https://developers.cloudflare.com/tunnel/get-started/): **Networking** > **Tunnels** > **Routes** > **Add route** > **Published application**, **Service URL**.
- [Tunnel origin parameters](https://developers.cloudflare.com/tunnel/reference/origin-parameters/): **Protect with Access** makes `cloudflared` require the Access JWT.
- [Publish a self-hosted application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/) (updated 2026-04-17): **Create new application** > **Self-hosted and private** > **Add public hostname**; deny by default.
- [Application paths](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/) (2026-04-17): empty path covers all paths; a wildcard path doesn't cover its parent; mid-path wildcard example spanning `/`; more specific rule wins.
- [Common policies: Bypass a public endpoint](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/): separate application for the path, Bypass / Include / Everyone.
- [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/): where the **Application Audience (AUD) Tag** is; it changes only if the application is recreated.
- Identity provider guides (e.g. [Generic OIDC](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/generic-oidc/)): team name under **Zero Trust** > **Settings** > **Team name and domain**.
- [Bot Fight Mode](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/) and [Security features interoperability](https://developers.cloudflare.com/waf/feature-interoperability/): Bot Fight Mode can't be skipped by rules; Super Bot Fight Mode can.
- [Skip options](https://developers.cloudflare.com/waf/custom-rules/skip/options/): the dashboard skip choices.
- [Configuration rules settings](https://developers.cloudflare.com/rules/configuration-rules/settings/): **Browser Integrity Check**, **I'm Under Attack**, **Email Obfuscation**.
- [Browser Integrity Check](https://developers.cloudflare.com/waf/tools/browser-integrity-check/): challenges missing or non-standard user agents.
- [Cloudflare HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/) and [Managed Transforms](https://developers.cloudflare.com/rules/transform/managed-transforms/reference/): `CF-Connecting-IP`; **Remove visitor IP headers** removes it.
