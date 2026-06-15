# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

# --- Desktop entry -----------------------------------------------------------
desktop_file = i18n.merge_file(
        input: '@0@.desktop.in'.format(application_id),
       output: '@0@.desktop'.format(application_id),
         type: 'desktop',
       po_dir: '../po',
      install: true,
  install_dir: datadir / 'applications',
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

# --- D-Bus activation service ------------------------------------------------
# The desktop entry is DBusActivatable (Cloudy is the system mailto:/.ics
# handler), so a matching D-Bus service file is required — Flatpak's exporter
# rejects the build without it, and it enables proper activation on the host.
dbus_conf = configuration_data()
dbus_conf.set('bindir', bindir)
configure_file(
         input: '@0@.service.in'.format(application_id),
        output: '@0@.service'.format(application_id),
  configuration: dbus_conf,
       install: true,
   install_dir: datadir / 'dbus-1' / 'services',
)

# --- AppStream metainfo ------------------------------------------------------
metainfo_file = i18n.merge_file(
        input: '@0@.metainfo.xml.in'.format(application_id),
       output: '@0@.metainfo.xml'.format(application_id),
         type: 'xml',
       po_dir: '../po',
      install: true,
  install_dir: datadir / 'metainfo',
)

appstream_util = find_program('appstreamcli', required: false)
if appstream_util.found()
  test('Validate metainfo', appstream_util,
       args: ['validate', '--no-net', '--explain', metainfo_file])
endif

# --- Nautilus (python) extension --------------------------------------------
# Installed to the system search path so an RPM/host install activates the
# file-manager integration automatically (and `dnf remove` drops it). In the
# Flatpak this lands at /app/share/...; the app copies it to the host on first
# run (the sandbox can't write the host path at build time). See application.py.
install_data(
  '..' / 'nautilus-extension' / 'cloudy_nautilus.py',
  install_dir: datadir / 'nautilus-python' / 'extensions',
)

# --- GSettings schema --------------------------------------------------------
install_data(
  '@0@.gschema.xml'.format(application_id),
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

# Optional build-time credential injection (release builds only). When any of
# the credential options is set, generate a GSettings vendor override that fills
# the empty schema defaults. A plain dev build sets none of these, so no
# override is produced and the build ships zero credentials. See meson.options.
ms_client_id         = get_option('ms_client_id')
google_client_id     = get_option('google_client_id')
google_client_secret = get_option('google_client_secret')
if ms_client_id != '' or google_client_id != '' or google_client_secret != ''
  cred_conf = configuration_data()
  cred_conf.set('MS_CLIENT_ID', ms_client_id)
  cred_conf.set('GOOGLE_CLIENT_ID', google_client_id)
  cred_conf.set('GOOGLE_CLIENT_SECRET', google_client_secret)
  configure_file(
           input: 'cloudy.gschema.override.in',
          output: '90_@0@.gschema.override'.format(application_id),
    configuration: cred_conf,
         install: true,
     install_dir: datadir / 'glib-2.0' / 'schemas',
  )
  message('Credential override WILL be installed (release build).')
endif

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('Validate schema', compile_schemas,
       args: ['--strict', '--dry-run', meson.current_source_dir()])
endif

# --- Blueprint UI ------------------------------------------------------------
# Compile .blp sources to .ui in the build tree, preserving the ui/ subpath so
# the GResource can reference them as "ui/<name>.ui".
blueprint_compiler = find_program('blueprint-compiler')

blueprints = custom_target('blueprints',
  input: files(
    'ui/window.blp',
  ),
  output: '.',
  command: [
    blueprint_compiler, 'batch-compile',
    '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@',
  ],
)

# --- GResource (UI etc.) -----------------------------------------------------
gnome.compile_resources(
  application_id,
  '@0@.gresource.xml'.format(application_id),
  gresource_bundle: true,
  dependencies: blueprints,
  install: true,
  install_dir: pkgdatadir,
)

subdir('icons')
