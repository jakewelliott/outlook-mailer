# Microsoft setup: free Azure account and app registration

This guide covers everything you do on Microsoft's side so outlook-mailer can send mail from your personal
@outlook.com account through Microsoft Graph. Nothing here costs money. Steps and links were checked against
Microsoft's documentation on 2026-11-11. Microsoft renames portal labels often, so if a label differs, look for the
closest match. Things that could not be confirmed are flagged with **(not verified)**.

## What you'll end up with

- A free Microsoft Entra directory (tenant). It comes with a free Azure account.
- An app registration called `outlook-mailer` in that directory.
- Its **Application (client) ID**, a GUID like `00001111-aaaa-2222-bbbb-3333cccc4444`. This is the value for the
  `MS_CLIENT_ID` Docker variable.

There is **no client secret, certificate or password** to copy. The app is a "public client" that signs in with a
one-time device code, so it has no secret and must not be given one.

**Time:** about 20 to 30 minutes. Most of it is the Azure sign-up.

**You need:**

- Your @outlook.com email address and password, with two-step verification working (phone or Authenticator app).
- A phone that can receive a text or call, for Azure's identity check.
- A credit or debit card, used **only** to verify your identity (see Part 1).
- A desktop browser.

## Part 0: check whether you already have a directory

If you've used Azure before, you might already have a directory and can skip Part 1.

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com) and sign in with your @outlook.com account.
2. If you get to the admin center home page and **Entra ID** > **App registrations** > **New registration** works,
   skip to Part 2.
3. If sign-in fails with an error such as `AADSTS16000` ("...doesn't exist in tenant..."), or app registrations
   say *"The ability to create applications outside of a directory has been deprecated"*, you have no directory.
   Do Part 1.

## Part 1: create a free Azure account

Since June 2024, Microsoft requires every new app registration to live in a directory. A personal account on its
own can't register apps any more. The simplest free way to get a directory is to sign up for Azure. A Microsoft 365
Personal or Family subscription does **not** include one.

**Which account to sign up with:** use your **@outlook.com** account. It's the simplest choice: one account, one
password. That account becomes the directory's administrator and the owner of the app registration, and it's the
same account that signs in to the app to send mail. A separate Microsoft account would work too, but it's one more
login to look after, and losing it would mean losing the directory.

### Sign up

1. Open the [Azure free account page](https://azure.microsoft.com/pricing/purchase-options/azure-account).
2. Select **Try Azure for free** (or **Start free**; the button label varies).
3. Sign in with your @outlook.com account.
4. Fill in the profile form (name, country, phone).
5. Complete the phone check (text or call).
6. Enter your card for **identity verification**. Microsoft says it charges nothing at sign-up. There "may be a
   temporary $1 authorization" on the card, which goes away.
7. Accept the agreement and select **Sign up**.
8. Wait until the Azure portal opens. Your new directory is usually called **Default Directory**.

If Azure says you're not eligible for a free account (for example, because you've had one before), **stop**. Don't
pick pay-as-you-go to get past it. That plan has no spending limit. Decide how to proceed first, then continue.

### Stay at $0

The free account comes with a Free Trial subscription: $200 of credit for 30 days and a **spending limit** that is
switched on by default. While the spending limit is on, Azure can't charge your card.

1. **Never select "Upgrade" or "Move to pay-as-you-go".** Upgrading removes the spending limit and makes your card
   the payment method. Ignore Azure's upgrade emails and banners.
2. **Don't remove the spending limit.** For a free account, removing it is the same as upgrading.
3. **Don't create Azure resources** (VMs, storage, databases and so on). outlook-mailer needs none. The app
   registration is part of the directory, not an Azure resource, and it's free.
4. **Don't buy Marketplace items or support plans.** Microsoft lists these, plus Entra ID P1/P2 licences, as things
   that can be billed separately even with the spending limit on.
5. **After 30 days:** if you haven't upgraded, Azure disables the subscription. That's expected and fine. The
   directory and the app registration aren't deleted when the subscription is disabled. **(Not verified:** Microsoft
   doesn't explicitly promise that personal-account sign-in to your app, or editing the registration, keeps working
   after the trial ends. Community reports say it does. This is why Part 2 should be done right away, inside the
   30 days.)
6. **Optional: budget alert.** With the spending limit on and no resources this is belt and braces, and a new
   subscription can take up to 48 hours before budgets are available. If you want one anyway:
   1. In the [Azure portal](https://portal.azure.com), open **Subscriptions** and select your subscription.
   2. Select **Budgets**, then **Add**.
   3. Enter a name, keep **Reset period** monthly, and set **Amount** to `1`.
   4. Select **Next**, add an alert at 100% of actual cost with your email address, and select **Create**.
   5. Add `azure-noreply@microsoft.com` to your safe senders so the alert doesn't go to Junk.

### Keep the directory alive

Microsoft blocks directories it considers inactive (sign-in error `AADSTS5000225: This tenant has been blocked due
to inactivity`). After 20 days of being blocked, the directory is deleted for good, along with the app registration.
Microsoft doesn't publish exactly what counts as activity **(not verified)**. A sensible habit: sign in to the
[Entra admin center](https://entra.microsoft.com) every couple of months and open the app registration.

## Part 2: register the application

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com) and sign in with your @outlook.com account.
2. If you belong to more than one directory, select the **Settings** (gear) icon in the top bar and switch to the
   one from Part 1 (for example **Default Directory**).
3. Browse to **Entra ID** > **App registrations**.
4. Select **New registration**.
5. **Name:** `outlook-mailer`.
6. **Supported account types:** select **Personal accounts only**. Older versions of the portal call this
   **Personal Microsoft accounts only**. The app signs in through the `/consumers` endpoint, which is for personal
   accounts only. (**Any Entra ID Tenant + Personal Microsoft accounts** would also work, but it allows more than we
   need.) Don't pick either single-tenant or multi-tenant option without personal accounts; those can't be used with
   @outlook.com.
7. **Redirect URI:** leave it empty. Device code sign-in doesn't use one.
8. Select **Register**.
9. The app's **Overview** page opens. Copy the **Application (client) ID**. This is your `MS_CLIENT_ID`.
   You don't need the **Directory (tenant) ID**.

### Turn on public client flows

Device code sign-in only works if the app is marked as a public client. This is off by default.

1. On the app's page, under **Manage**, select **Authentication**.
2. Find **Allow public client flows**. Where it is depends on which version of the page you see:
   - Newer layout (tabs at the top, such as **Redirect URI configuration** and **Settings**): open the **Settings**
     tab. **(Not verified** on a Microsoft page; third-party guides describe it this way.)
   - Older layout (one long page): scroll down to **Advanced settings**.
3. Set **Allow public client flows** to **Yes** (or **Enabled**). Some versions word it
   **Enable the following mobile and desktop flows**.
4. Select **Save**.

Don't add a platform or redirect URI on this page. They aren't needed.

### Add the API permissions

1. Under **Manage**, select **API permissions**.
2. **User.Read** (Microsoft Graph, Delegated) is usually there already. Keep it.
3. Select **Add a permission** > **Microsoft Graph** > **Delegated permissions**.
4. Search for `Mail.Send`, tick **Mail.Send**, and select **Add permissions**.
5. Select **Add a permission** > **Microsoft Graph** > **Delegated permissions** again.
6. Under **OpenId permissions**, tick **offline_access** and select **Add permissions**. This allows a refresh
   token, so you don't have to sign in again every hour.
7. Check that the list shows exactly three Microsoft Graph **Delegated** permissions: `Mail.Send`, `User.Read`,
   `offline_access`. Don't add any **Application** permissions. Graph doesn't support them for personal accounts.

**Admin consent isn't needed.** Microsoft marks all three permissions as "Admin consent required: No", and with a
personal account you approve them yourself during the app's sign-in. Skip the
**Grant admin consent for ...** button.

### Things not to do

- Don't create anything under **Certificates & secrets**. The app has no secret and shouldn't have one.
- Don't change **Supported account types** later. If it's wrong, it's simplest to delete the registration and
  create a new one.

## Part 3: put the client ID into the app

1. Set the Docker variable `MS_CLIENT_ID` to the Application (client) ID from Part 2.
2. Leave `MS_AUTHORITY` unset (the default is `https://login.microsoftonline.com/consumers`).
3. Restart the container.

## Part 4: what happens later in the app (sign-in)

You do this once, from the app's Microsoft account page:

1. Select the button to connect your Microsoft account. The app shows a short **code** and a **link**.
2. Open the link on any device. Use the link the app shows. It's the one Microsoft returns, normally
   `https://microsoft.com/link` or `https://microsoft.com/devicelogin`. Both are genuine Microsoft pages:
   `microsoft.com/link` goes straight to the personal-account sign-in page (`login.live.com`), and
   `microsoft.com/devicelogin` goes to the general Microsoft sign-in page. Which one Microsoft returns for this app
   is **(not verified)** until the first real sign-in.
3. Enter the code within 15 minutes. After that it expires and you start again.
4. Sign in with your @outlook.com account. Microsoft asks for your password again even if you're already signed in.
   That's expected for personal accounts.
5. Confirm that you're signing in to **outlook-mailer** when Microsoft asks.
6. Review the permissions (send mail as you, read your profile, keep access) and select **Accept**. The app may be
   marked as unverified, which is normal for a personal app.
7. Go back to the app. It shows the connected address within a few seconds.

After that, the app renews its access itself. You only reconnect if you change your Outlook password, remove the
app's access, or the app goes about 90 days without being used.

### Revoking access

- **From the app:** disconnect on the app's Microsoft account page. This deletes the stored token.
- **From your Microsoft account:** open [account.live.com/consent/Manage](https://account.live.com/consent/Manage),
  sign in, find **outlook-mailer**, select **Edit**, then **Remove these permissions**. You can also reach the same
  list from [account.microsoft.com](https://account.microsoft.com) > **Privacy** >
  **Apps and services that can access your data**. (The link works and opens a sign-in. The labels on the page are
  from third-party guides and are **not verified**.)
- **Permanently:** delete the app registration in the Entra admin center (**App registrations** > `outlook-mailer` >
  **Delete**). The client ID then stops working everywhere.

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| *"The ability to create applications outside of a directory has been deprecated"*, or `AADSTS16000` when you open the Entra admin center | Your account has no directory | Do Part 1, then switch to that directory (Part 2, step 2). |
| `AADSTS7000218: The request body must contain the following parameter: 'client_assertion' or 'client_secret'` (`invalid_client`) | **Allow public client flows** is off, so Microsoft treats the app as one that needs a secret | Turn on public client flows (Part 2) and **Save**. Don't add a secret. |
| `AADSTS70002` (`invalid_client`) | Microsoft's reference describes this as a client-secret problem. It can show up when the app is set up as a confidential client | Same fix as the row above. Make sure no secret exists under **Certificates & secrets**. |
| `unauthorized_client`, often with *"The client does not exist or is not enabled for consumers"* (this wording is **not verified** on a Microsoft page) | Wrong **Supported account types** (no personal accounts), or a typo in `MS_CLIENT_ID` | Check the client ID. Open the registration's **Overview** and check **Supported account types**. If it doesn't include personal accounts, create a new registration with **Personal accounts only**. |
| `AADSTS9002332: Application ... is configured for use by Microsoft Entra users only. Please do not use the /consumers endpoint` | The registration was created for work/school accounts only | Create a new registration with **Personal accounts only**, then update `MS_CLIENT_ID`. |
| `AADSTS700016: ... application wasn't found in the directory/tenant` | Wrong client ID, or the registration was deleted | Copy the **Application (client) ID** again from **Overview**. Don't use the tenant ID or an object ID. |
| `AADSTS50020` or `AADSTS500208` (account doesn't exist in tenant, or not a valid login domain for the account type) | You signed in with a work or school account, or the app isn't set to personal accounts | Sign in with your @outlook.com account. Check **Supported account types**. |
| `AADSTS50194: ... isn't configured as a multitenant application. Usage of the /common endpoint isn't supported` | `MS_AUTHORITY` was changed to `/common` | Remove `MS_AUTHORITY` so it defaults to `/consumers`. |
| `AADSTS90133: Device Code flow is not supported under /common or /consumers` | Old message. Microsoft's current device code docs list `/consumers` as supported | Check that the registration allows personal accounts and that public client flows are on. If it still happens, note the full error. |
| Code rejected, or `expired_token` | More than 15 minutes passed before the code was entered | Start the sign-in again to get a new code. |
| `authorization_declined` or `AADSTS65004` (user declined consent) | **Cancel** was selected on the permissions screen | Start again and select **Accept**. |
| `invalid_grant`, `AADSTS70008`, `AADSTS700082` (refresh token expired from inactivity) or `AADSTS50173` (grant revoked) | The token is no longer valid: about 90 days unused, a password change, or access removed | Reconnect from the app's Microsoft account page (Part 4). |
| `AADSTS5000225: This tenant has been blocked due to inactivity` | Microsoft blocked the directory as inactive | Contact Microsoft through the [global support phone numbers](https://support.microsoft.com/topic/global-customer-service-phone-numbers-c0389ade-5640-e588-8b0e-28de8afeb3f2) within 20 days. After that the directory is deleted and the setup has to be redone in a new one. |
| Graph send fails with access denied (`403`) | `Mail.Send` wasn't granted when you signed in | Check the API permissions (Part 2), then reconnect so you're asked to consent again. |
| Sends start failing and Outlook.com says your account is temporarily blocked, or there was "unusual activity" | Outlook.com's abuse protection. Bulk sending to many new recipients from a free account can trigger it | 1. Pause the campaign. 2. Sign in at [outlook.live.com](https://outlook.live.com) and follow the prompts (usually a security code, sometimes a password change). 3. If that doesn't help, use the [sign-in helper](https://aka.ms/sign-in-helper) and Microsoft's [Unblock my Outlook.com account](https://support.microsoft.com/accounts-billing/manage/unblock-my-outlook-com-account) page. 4. Also check your mailbox and OneDrive aren't full, because that blocks sending too. 5. Resume later with a lower `SEND_RATE_PER_MINUTE` / `DAILY_SEND_CAP`. The exact Graph error code for a blocked account is **(not verified)**. |
| Azure says you're not eligible for the free account | Usually a previous free account or trial on the same identity | Don't choose pay-as-you-go to get past it (see Part 1). Check Part 0 first: an earlier Azure sign-up may already have left you a directory. |

## References

Microsoft pages checked on 2026-11-11 (the "updated" date is Microsoft's own page metadata):

- [How to register an app in Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app) (updated 2026-06-15): navigation, **Supported account types** labels, client ID.
- [How to add a redirect URI](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri) (2026-06-15): current **Authentication** page with the **Redirect URI configuration** tab.
- [Configure desktop apps that call web APIs](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration) (2026-06-15): device code needs no redirect URI; **Advanced settings** > **Allow public client flows** > **Yes**.
- [Public and confidential client apps](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications) (2026-06-15): public clients can't have secrets; device code needs public client flows.
- [AADSTS7000218 troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/confidential-client-application-authentication-error-aadsts7000218) (2026-02-18): cause and the **Enable the following mobile and desktop flows** label.
- [OAuth 2.0 device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code) (2026-06-15): `/consumers` supported, 15-minute code lifetime, extra sign-in for personal accounts, `offline_access` for refresh tokens, polling errors.
- [Acquire a token using device code flow](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-acquire-token-device-code-flow) (2026-06-15): `microsoft.com/devicelogin` example and the old `AADSTS90133` message.
- [Error codes reference](https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes) (2026-11-06): wording for the AADSTS codes, `invalid_grant` and `unauthorized_client` in the troubleshooting table.
- [Updates and breaking changes](https://learn.microsoft.com/en-us/entra/identity-platform/reference-breaking-changes) (2026-08-26): June 2024 "Applications must be registered in a directory"; June 2021 device code confirmation prompt.
- [Microsoft Q&A: moving an app created under a personal account](https://learn.microsoft.com/en-us/answers/questions/2127352/how-to-move-an-azure-application-created-under-a-p): exact wording of the "outside of a directory" message.
- [user: sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0) (2026-06-19): delegated `Mail.Send` supported for personal Microsoft accounts.
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference): `Mail.Send`, `User.Read`, `offline_access` all "AdminConsentRequired: No".
- [Permissions and consent overview](https://learn.microsoft.com/en-us/entra/identity-platform/permissions-consent-overview) (2026-06-25): users consent to delegated permissions for their own data.
- [Refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens): 90-day lifetime (checked 2026-10-25, see [RESEARCH.md](RESEARCH.md)).
- [Azure free account](https://azure.microsoft.com/pricing/purchase-options/azure-account): $200 for 30 days, card not charged at sign-up, possible $1 hold, disabled if not upgraded.
- [Reactivate a disabled Azure subscription](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/subscription-disabled) (2026-06-11): Free Trial disabled after 30 days to avoid charges.
- [Azure spending limit](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/spending-limit) (2026-04-30): on by default for free accounts; Marketplace items and Entra P1/P2 billed separately.
- [Create and manage budgets](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets) (2025-09-26): budget steps and the 48-hour delay for new subscriptions.
- [Create a new tenant](https://learn.microsoft.com/en-us/entra/fundamentals/create-new-tenant) (2026-08-21): free and trial customers can't create more tenants; sign up for a free Azure account instead.
- [Delete a Microsoft Entra tenant](https://learn.microsoft.com/en-us/entra/identity/users/directory-delete-howto) (2026-03-25): a directory is separate from its subscriptions.
- [Troubleshoot inaccessible tenants](https://learn.microsoft.com/en-us/entra/fundamentals/inaccessible-tenant) (2026-04-09): `AADSTS5000225`, 20-day reactivation window.
- [Microsoft Q&A: Entra ID Free after Azure free-trial expiry](https://learn.microsoft.com/en-us/answers/questions/6037952/entra-id-free-after-azure-free-trial-expiry-person) (2026-10-15): open question; the only answer is AI-generated. This is the source of the "not verified" note in Part 1.
- [Unblock my Outlook.com account](https://support.microsoft.com/accounts-billing/manage/unblock-my-outlook-com-account) and [Can't send or receive email in Outlook.com](https://support.microsoft.com/en-us/office/can-t-send-or-receive-email-in-outlook-com-d39e3341-8d79-4bf1-b3c7-ded602233642): blocks for unusual activity, the sign-in helper, full storage.

Redirects checked with `curl` on 2026-11-11: `microsoft.com/link` → `login.live.com/oauth20_remoteconnect.srf`;
`microsoft.com/devicelogin` → `login.microsoftonline.com/common/oauth2/deviceauth`;
`account.live.com/consent/Manage` → consumer sign-in.
