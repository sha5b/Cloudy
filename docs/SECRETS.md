<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Secrets & making the repo public

Short version: **the repo is safe to publish.** Only a *public* client ID is
committed; real secrets live outside the source tree.

## What is and isn't a secret

| Value | Secret? | In the public repo? |
|---|---|---|
| **Microsoft client ID** (`dcd8ee18-…`) | **No.** Public/native OAuth clients have no confidential secret; the ID is meant to ship in the app (so do rclone, GitHub CLI, Thunderbird). | ✅ Baked into the gschema default — fine. |
| **Google client ID** | Not really (sent in every request, visible to users). | Kept out for cleanliness; supplied via env/secrets file. |
| **Google client secret** | ⚠️ Google calls desktop-app secrets "not confidential," but treat it as sensitive: it can be abused for quota/impersonation. | **Never commit.** |
| **User OAuth tokens** | **Yes.** | Never touch git — stored per-user in libsecret. |

## Where real credentials live (never in git)

The app reads `CLOUDY_*` environment variables. On startup it also loads them
from a file **outside the repo**:

```
~/.config/cloudy/secrets.env
```

Example (this file is NOT in the repository):

```sh
# ~/.config/cloudy/secrets.env
CLOUDY_GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
CLOUDY_GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
# CLOUDY_MS_CLIENT_ID is already baked in; override here only to use your own.
```

`.gitignore` also blocks `secrets.env`, `*.secret`, `*.local`,
`client_secret*.json` as a backstop.

## Shipping a build with credentials (release pipeline)

For a distributed Flatpak, inject the Google values at **build time** from a
secret store (e.g. GitHub Actions repository secrets), never from committed
source:

1. Store `CLOUDY_GOOGLE_CLIENT_ID` / `CLOUDY_GOOGLE_CLIENT_SECRET` as CI secrets.
2. The release job writes them into the gschema default (or a generated config)
   during the build.
3. The public source keeps empty Google defaults.

> Note: any secret shipped inside a desktop app is ultimately extractable from
> the binary — this is inherent to native OAuth and why Google classifies the
> desktop secret as non-confidential. Keeping it out of *source* still matters:
> it prevents scraping, keeps history clean, and lets you rotate without a
> commit.

## If a secret ever leaks

1. Revoke/rotate it in the provider console (Entra / Google Cloud).
2. Replace the value in your secrets file / CI.
3. If it was committed, rotating is what matters — scrubbing history is
   secondary (assume leaked = compromised).
