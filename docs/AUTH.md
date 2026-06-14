<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Authentication

All Microsoft 365 / OneDrive / Exchange access goes through **Microsoft Graph**
with **OAuth2**. Google access uses **Google OAuth2** + the Gmail/Calendar APIs.
Tokens are stored via **libsecret**, never in plaintext.

## Microsoft Graph (Entra ID app registration)

> **For end users this is already done** — Clouddrive ships a multi-tenant
> client ID, so signing in is one click (browser → consent). The steps below are
> only for the project maintainer registering that shared app, or for users who
> set `CLOUDDRIVE_MS_CLIENT_ID` / the `microsoft-client-id` setting to their own.

1. In the Microsoft Entra admin center, **register an application**; set
   **Supported account types** to *Accounts in any organizational directory and
   personal Microsoft accounts* (multi-tenant).
2. Add a **"Mobile and desktop applications"** platform with the loopback
   redirect URI **`http://localhost`** (MSAL's interactive flow uses a loopback
   server; also add
   `https://login.microsoftonline.com/common/oauth2/nativeclient`). This is a
   **public client** — no client secret.
3. Enable **"Allow public client flows"**.
4. Delegated scopes (request the subset each module needs):
   - Files: `Files.ReadWrite.All`, `Sites.ReadWrite.All`
   - Mail/Calendar/Contacts: `Mail.ReadWrite`, `Calendars.ReadWrite`,
     `Contacts.ReadWrite`
   - Always: `User.Read`, `offline_access`, `openid`, `profile`

### Auth flow — system browser + loopback (primary)

The user clicks **Sign In** and we run the **authorization code + PKCE** flow
through their **default web browser**:

1. Start a throwaway loopback HTTP server on `http://127.0.0.1:<random-port>`.
2. Open the system browser (via the **OpenURI portal** / `Gtk.UriLauncher`) at
   the provider's real consent page, with `redirect_uri` = that loopback URL.
3. The user authenticates and consents in the browser (no credentials ever
   touch Clouddrive).
4. The provider redirects to the loopback URL; the server captures the `code`,
   and MSAL exchanges it (with the PKCE verifier) for tokens.
5. The loopback server shuts down; we show the account as signed in.

**Device code flow** (`initiate_device_flow` / `acquire_token_by_device_flow`)
is kept as a **fallback** for headless/remote sessions where no browser is
available ("enter this code at microsoft.com/devicelogin").

Use **MSAL for Python** (`msal`). Persist its `SerializableTokenCache` into
libsecret; refresh silently with `acquire_token_silent`. Always request
`offline_access` for long-lived refresh tokens.

### One-click setup — we ship the client ID

To avoid making users register an Azure/Google app, Clouddrive ships a single
**multi-tenant public client ID** owned by the project (the same pattern rclone,
abraunegg, and GNOME Evolution use). Sign-in is then one click → browser →
consent → done, with **no manual app registration**. The client ID is public by
design (public clients hold no secret); it is configurable via GSettings/env for
users who prefer their own registration.

### Business / tenant caveats

- `*.All` and `Sites.ReadWrite.All` scopes frequently require **tenant admin
  consent**. Treat "an admin must approve this app" as a first-class onboarding
  state.
- Some tenants are "unmanaged" and block third-party apps (`AADSTS65005`) until
  an admin claims the domain.
- **AIP-protected files** report mismatched size/hash via Graph and may fail
  integrity checks — handle in status/error UI.

## Google (Gmail + Calendar)

Same UX as Microsoft: **system browser + loopback + PKCE**, implemented directly
on urllib (no Google SDKs). Tokens (access + refresh) stored in libsecret;
`access_token` refreshes transparently. Configurable via the `google-client-id`
setting / `CLOUDDRIVE_GOOGLE_CLIENT_ID` env.

One-time setup for the project maintainer:

1. Create a **Google Cloud project** and an **OAuth client** (Desktop app type).
2. Enable the Gmail API and Google Calendar API.
3. Scopes (read-only for now): `gmail.readonly`, `calendar.readonly`, plus
   `openid email profile`.
4. The installed-app flow uses a loopback redirect (`http://localhost:<port>`)
   with PKCE; the refresh token is kept in libsecret.

## Why not EWS / Evolution-EWS?

Exchange Web Services soft-blocks for non-Microsoft apps on **2026-10-01** and is
**fully retired 2027-04-01** ("no exceptions and no re-enablement"). Basic Auth
was retired in 2022 and `ApplicationImpersonation` deprecated Feb 2025. Anything
built on EWS has a hard expiry, so Clouddrive targets **Graph** for Exchange.

## Secret storage

`core/secrets.py` wraps libsecret's simple API. Inside the Flatpak sandbox this
transparently uses the **Secret Service portal** with a per-app local keyring —
no broad `--talk-name=org.freedesktop.secrets` hole required.
