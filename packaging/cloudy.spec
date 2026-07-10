# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
#
# RPM spec for Cloudy (Fedora 44 / GNOME 50).
#
# Build-time OAuth credentials (optional) are passed as rpmbuild defines and
# never live in this committed spec:
#   rpmbuild --define 'ms_client_id ...' --define 'google_client_id ...' \
#            --define 'google_client_secret ...' ...
# `make rpm` reads them from .env automatically. Without them the package builds
# fine but ships no credentials (sign-in then needs per-user CLOUDY_* config).

%global appid io.github.sha5b.Cloudy

Name:           cloudy
Version:        0.3.0
Release:        1%{?dist}
Summary:        Use OneDrive, SharePoint and unified mail on your desktop

License:        GPL-3.0-or-later
URL:            https://github.com/sha5b/Cloudy
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

# Toolchain (meson + the validators run in %%check).
BuildRequires:  meson >= 1.0.0
BuildRequires:  ninja-build
BuildRequires:  gcc
BuildRequires:  python3-devel
BuildRequires:  blueprint-compiler
BuildRequires:  gettext
BuildRequires:  glib2-devel
BuildRequires:  desktop-file-utils
BuildRequires:  /usr/bin/appstreamcli
# Needed so blueprint-compiler can resolve the GTK/Adw typelibs at build.
BuildRequires:  gtk4
BuildRequires:  libadwaita

# Runtime. PyGObject + the GTK/Adw/Secret typelibs come from these.
Requires:       python3
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       glib2
Requires:       libsecret
Requires:       python3-msal
Requires:       hicolor-icon-theme

# Optional capabilities — the app degrades gracefully without them.
# rclone: file mounts (also self-provisioned rootlessly at runtime if absent).
# webkitgtk6.0: rich HTML mail rendering. evolution-data-server: EDS calendar
# mirror. nautilus-python: the file-manager emblems/menu extension.
Recommends:     rclone
Recommends:     webkitgtk6.0
Suggests:       evolution-data-server
Suggests:       nautilus-python

%description
Cloudy is a GNOME-native super-app that makes Microsoft 365 (OneDrive,
Teams/SharePoint, Mail, Calendar) and Google (Gmail, Calendar, Drive) easy to
use on Fedora. Files are live rclone FUSE mounts that appear in Nautilus and an
in-app browser; mail and calendar are unified across providers. It orchestrates
proven Linux backends rather than reinventing them, behind one adaptive,
modular Libadwaita interface.

%prep
%autosetup -n %{name}-%{version}

%global _vpath_builddir %{_target_platform}

%build
# Explicit meson calls (rather than the %%meson macro) so the spec builds on a
# host where meson is on PATH but meson-rpm-macros isn't installed. For a noarch
# pure-Python+data package this is equivalent; no compiler flags to honour.
# Credential defines are empty unless passed on the rpmbuild command line.
meson setup %{_vpath_builddir} \
    --prefix=%{_prefix} \
    --libdir=%{_libdir} \
    --buildtype=plain \
    --wrap-mode=nodownload \
    -Dms_client_id="%{?ms_client_id}" \
    -Dgoogle_client_id="%{?google_client_id}" \
    -Dgoogle_client_secret="%{?google_client_secret}"
meson compile -C %{_vpath_builddir}

%install
# --skip-subprojects: don't install the build-only blueprint-compiler subproject
# (used here when the host lacks a system blueprint-compiler) into the package.
DESTDIR=%{buildroot} meson install -C %{_vpath_builddir} --skip-subprojects

%check
# desktop/schema/metainfo (and blueprint, if the subproject is present) validators.
meson test -C %{_vpath_builddir} --print-errorlogs

# Schema, icon-cache and desktop-database refreshes are handled automatically by
# the file triggers in glib2 / gtk-update-icon-cache / desktop-file-utils on
# modern Fedora — no scriptlets needed here.

%files
%license COPYING
%doc README.md CHANGELOG.md
%{_bindir}/%{name}
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/dbus-1/services/%{appid}.service
%{_datadir}/metainfo/%{appid}.metainfo.xml
%{_datadir}/glib-2.0/schemas/%{appid}.gschema.xml
%if "%{?ms_client_id}%{?google_client_id}%{?google_client_secret}" != ""
%{_datadir}/glib-2.0/schemas/90_%{appid}.gschema.override
%endif
%{_datadir}/nautilus-python/extensions/cloudy_nautilus.py
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{appid}-symbolic.svg
%{_datadir}/icons/hicolor/*/apps/%{appid}.png
%{_datadir}/%{name}/

%changelog
* Fri Jul 10 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.3.0-1
- Group chats: add several people to a new chat, or add people to a 1:1 (starts a group).
- New chat composer autocompletes and accepts multiple recipients.
- Fix SharePoint/OneDrive Office uploads never reaching the server (--ignore-size/--ignore-checksum).
- Files view shows live per-drive sync status; rclone mounts now log to ~/.local/share/cloudy/logs.
- Fix GNOME Calendar times shifted by the local offset (Windows zone names now map to the local zone).
- Background calendar sync mirrors edits/deletes to GNOME, not just newly-added events.
- Reconcile forgotten mounts and stale Nautilus bookmarks at startup so writes can't vanish.

* Tue Jul 07 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.9-1
- Fix stale calendar/dashboard data: every event and mail write now invalidates the caches.
- Fix Graph TimeZoneNotSupportedException ('CEST'): resolve the IANA zone from /etc/localtime.
- Fix detail/compose windows never finishing loading (async callback guard dropped toplevels).
- Meeting invites accepted from mail now sync to your calendar; cancellations can remove the event.
- Mail organization: right-click to mark unread, flag, move to folder; Save draft + resume drafts.
- Harden Gmail/Graph parsers, .ics unfolding, and account-store loading against malformed input.
- Split the Graph client into per-domain modules; dedupe date parsing and list navigation helpers.

* Mon Jun 29 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.8-1
- Per-account API client cache; Mail/Chat lists refresh in place instead of rebuilding.
- Microsoft 365 share links resolve local paths to the correct drive/item.
- Graph calendar timezone handling, calendar-id routing, and OneNote pagination.
- RFC 5545 iCalendar escaping, Google OAuth receiver cleanup, and pinned rclone checksums.
- Code cleanup: extracted graph_markup, file_browser_utils, chat_avatar; removed abraunegg stub.

* Fri Jun 26 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.7-1
- Mail: Reply all and Forward (forward carries the original attachments).
- Per-account email signature (Preferences -> Accounts), added to new mail,
  replies and forwards.
- Teams chats can send any file type, not just images (max 10 per message).
- Microsoft and Google errors shown in plain language via a shared formatter.
- Email links and meeting Join buttons open reliably in the Flatpak sandbox.
- Reading mail clears its desktop notification and updates the unread badge;
  deleting a Teams message updates the thread immediately.
- Activity overview lays out in balanced columns (no large empty gaps).
- Nautilus integration no longer blocks the file manager on per-file D-Bus
  lookups (local prefix check instead).

* Mon Jun 22 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.6-1
- Mounted drives auto-remount at startup and reconnect if their daemon dies
  (health watchdog).
- Mail: server-side mailbox search (press Enter) alongside the instant filter.
- File browser: multi-select with Copy to.../Move to.../Trash, and an
  in-folder name filter.

* Sun Jun 21 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.5-1
- Mail headers now show the sender's full address plus To/Cc/Bcc recipients,
  with every address a click-to-copy link.
- Double-click a message to open it in its own window.
- Fixed Chat presence dots that stayed "online" for contacts who had gone
  offline. Internal dedup/cleanup across the mail/chat/Teams surfaces.

* Thu Jun 18 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.4-1
- Calendar RSVP (Accept/Tentative/Decline) now works for both Microsoft and
  Google; unanswered invites show in the calendar dimmed but clickable.
- Meeting-invite emails carry Accept/Decline buttons that send a standard
  calendar reply (iMIP) to the organiser.
- New Activity tab: a notifier feed of recent mail, invites and chats, plus
  Teams-style "reacted to your message" / "mentioned you".
- Image viewer gains scroll-zoom and drag-pan; multi-image chat messages show
  as a wrapping gallery. Optional read-receipt request when composing mail.

* Wed Jun 17 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.3-1
- Chat: live conversation list (new messages bump to the top), clickable reply
  quotes that jump to the original message, flat avatars, instant scroll on send.
- Large chat/OneNote images are downscaled while decoding so they can't crash the
  renderer. Replies no longer show as a bare "attachment".
- New General setting to toggle the Nautilus file-manager integration.
- Chat composer: attach multiple images at once; sending more than one image no
  longer fails with a Graph "invalid payload" error (images are downscaled and
  normalised before upload). A failed send stays put with a Retry button, and the
  open conversation bumps to the top of the list on send/receive.
- Bug-fix sweep across Mail/Calendar/Files/Dashboard (escaping, KeyErrors, a
  popover leak) and dead-code/cleanup pass.

* Tue Jun 16 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.2-1
- Notification attention controls (DND, quiet hours, relevance level, per-chat/
  channel mute) with batched digest alerts; Google multi-calendar and Drive
  sources (My Drive / Shared with me / Shared Drives); command palette (Ctrl+K);
  persistent offline cache; Dashboard Activity feed; headless logic test suite.

* Mon Jun 15 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.1-1
- Teams tab: Teams → channels with a channel Conversation (posts + threaded
  replies) and a Notes tab backed by the team's OneNote notebook (read +
  create/edit). Flatpak build no longer depends on gitlab.gnome.org.

* Mon Jun 15 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.2.0-1
- Chat (Teams chats + Google Chat), RPM/Flatpak packaging, shared/group
  Mail & Calendar sources, calendar redesign, image viewer + rich-text editor.

* Sun Jun 14 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.1.0-1
- Initial RPM packaging.
