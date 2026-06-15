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
Version:        0.1.0
Release:        1%{?dist}
Summary:        Use OneDrive, SharePoint and unified mail on your desktop

License:        GPL-3.0-or-later
URL:            https://github.com/sha5b/Clouddrive-Fedora
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
* Sun Jun 14 2026 Shahab Nedaei <ned.tabulov@gmail.com> - 0.1.0-1
- Initial RPM packaging.
