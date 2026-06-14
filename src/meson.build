# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements

moduledir = pkgdatadir / 'cloudy'

# Configured launcher script installed as `cloudy` on PATH.
conf = configuration_data()
conf.set('PYTHON', python.full_path())
conf.set('VERSION', meson.project_version())
conf.set('APP_ID', application_id)
conf.set('localedir', localedir)
conf.set('pkgdatadir', pkgdatadir)

configure_file(
         input: 'cloudy.in',
        output: 'cloudy',
  configuration: conf,
       install: true,
   install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

# Install the Python package next to the GResource bundle.
install_subdir('cloudy', install_dir: pkgdatadir)
