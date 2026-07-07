# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

project('cloudy', 'c',
          version: '0.2.9',
    meson_version: '>= 1.0.0',
  default_options: [ 'warning_level=2', 'werror=false', ],
)

i18n = import('i18n')
gnome = import('gnome')

# Reverse-DNS application id used across the .desktop, metainfo, gschema,
# GResource and D-Bus surfaces. Change this single value to rebrand.
application_id = 'io.github.sha5b.Cloudy'

# Make the values available to configured files (launcher, desktop, etc.).
prefix     = get_option('prefix')
bindir     = prefix / get_option('bindir')
datadir    = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()
localedir  = prefix / get_option('localedir')

python = import('python').find_installation('python3')

subdir('data')
subdir('src')
subdir('po')
subdir('tests')

gnome.post_install(
     glib_compile_schemas: true,
    gtk_update_icon_cache: true,
  update_desktop_database: true,
)
