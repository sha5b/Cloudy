#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Generate a LOCAL Flatpak manifest with build-time credentials injected.

The committed manifest (io.github.sha5b.Cloudy.yml) carries NO credentials.
For a local test build we copy it and add `-D<cred>=...` config-opts to the
`cloudy` module from CLOUDY_* env vars (loaded from .env by the Makefile), and
pin the `dir` source to an absolute repo path so the manifest can live under
_build/. The generated file contains secrets, so it is written under _build/
(gitignored) and must never be committed.

Usage: flatpak-local-manifest.py <src-manifest> <repo-root> <out-manifest>
"""

from __future__ import annotations

import os
import sys

import yaml


def main() -> int:
    src, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    with open(src, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)

    cred_opts = []
    for opt, env in (
        ("ms_client_id", "CLOUDY_MS_CLIENT_ID"),
        ("google_client_id", "CLOUDY_GOOGLE_CLIENT_ID"),
        ("google_client_secret", "CLOUDY_GOOGLE_CLIENT_SECRET"),
    ):
        val = os.environ.get(env, "")
        if val:
            cred_opts.append(f"-D{opt}={val}")

    for module in manifest.get("modules", []):
        if not isinstance(module, dict) or module.get("name") != "cloudy":
            continue
        if cred_opts:
            module.setdefault("config-opts", []).extend(cred_opts)
        # Pin the dir source to an absolute path (manifest now lives elsewhere).
        for source in module.get("sources", []):
            if isinstance(source, dict) and source.get("type") == "dir":
                source["path"] = repo_root
        break

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False, default_flow_style=False)
    print(f"wrote {out} ({'with' if cred_opts else 'NO'} credentials)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
