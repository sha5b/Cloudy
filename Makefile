# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
#
# Convenience wrapper around Meson / Flatpak for reproducible builds.
# See docs/BUILDING.md for details.

APP_ID      := io.github.sha5b.Cloudy
VERSION     := $(shell sed -n "s/.*version: '\([0-9.]*\)'.*/\1/p" meson.build | head -1)
BUILDDIR    := _build
PREFIX      := $(CURDIR)/_install
FLATPAK_DIR := _build/flatpak

SCHEMA_DIR  := $(PREFIX)/share/glib-2.0/schemas

NAUTILUS_EXT_DIR := $(HOME)/.local/share/nautilus-python/extensions

# RPM build tree (self-contained under _build/, no ~/rpmbuild needed).
RPM_TOP     := $(CURDIR)/_build/rpm
RPM_TARBALL := $(RPM_TOP)/SOURCES/cloudy-$(VERSION).tar.gz
SPEC        := packaging/cloudy.spec

# Distributable artifacts. NOTE: these embed baked credentials (per the
# bake-at-build-time model), so RELEASE_DIR is gitignored — never commit it.
RELEASE_DIR    := release
FLATPAK_REPO   := _build/flatpak/repo
FLATPAK_BUNDLE := $(RELEASE_DIR)/$(APP_ID).flatpak

# Build-time credentials, sourced from .env when present. Both RPM and Flatpak
# read these; absent .env -> a credential-free build (no secrets, manual setup).
# Wrapped so the secret only enters the recipe shell, never the make environment.
LOAD_ENV    := set -a; [ -f .env ] && . ./.env; set +a;

.PHONY: all bootstrap setup build install run clean distclean \
        flatpak flatpak-run flatpak-test lint test test-unit install-nautilus \
        uninstall-nautilus rpm srpm dist-tarball release flatpak-bundle

all: build

## Install every dependency on Fedora 44 (toolchain + backends + flatpak)
bootstrap:
	./scripts/bootstrap-fedora.sh --all

## Configure the Meson build into $(BUILDDIR)
setup:
	meson setup $(BUILDDIR) --prefix="$(PREFIX)"

## Compile (configures first if needed)
build:
	@test -d $(BUILDDIR) || $(MAKE) setup
	meson compile -C $(BUILDDIR)

## Install into the local prefix
# Prune the previously-installed Python package first: meson's install_subdir
# copies but never deletes, so renamed/removed modules would otherwise linger
# and be discovered as phantom providers.
install: build
	rm -rf "$(PREFIX)/share/cloudy/cloudy"
	find src -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	meson install -C $(BUILDDIR)

## Build, install, and run locally (no sandbox)
run: install
	GSETTINGS_SCHEMA_DIR="$(SCHEMA_DIR)" $(PREFIX)/bin/cloudy

## Run the Meson test suite (schema/desktop/metainfo validation + logic units)
test: build
	meson test -C $(BUILDDIR) --print-errorlogs

## Run only the headless logic unit tests (fast; no build needed)
test-unit:
	PYTHONPATH=src:tests/unit python3 -m unittest discover -s tests/unit -p 'test_*.py'

## --- RPM ----------------------------------------------------------------
## Reproducible source tarball (excludes secrets, build cruft, VCS).
dist-tarball:
	@mkdir -p $(RPM_TOP)/SOURCES $(RPM_TOP)/SPECS $(RPM_TOP)/BUILD \
	          $(RPM_TOP)/BUILDROOT $(RPM_TOP)/RPMS $(RPM_TOP)/SRPMS
	@rm -rf $(RPM_TOP)/cloudy-$(VERSION)
	@rsync -a \
	  --exclude='.git' --exclude='_build' --exclude='_install' \
	  --exclude='.flatpak-builder' --exclude='.env' --exclude='.env.*' \
	  --exclude='__pycache__' --exclude='*.py[co]' --exclude='*.flatpak' \
	  --exclude='po/*.mo' --exclude='po/*.gmo' --exclude='subprojects/*/.git' \
	  ./ $(RPM_TOP)/cloudy-$(VERSION)/
	@tar -C $(RPM_TOP) -czf $(RPM_TARBALL) cloudy-$(VERSION)
	@rm -rf $(RPM_TOP)/cloudy-$(VERSION)
	@echo "Source tarball: $(RPM_TARBALL)"

## Build a binary + source RPM into $(RPM_TOP)/RPMS, baking creds from .env.
# --nodeps lets this run without root-installed BuildRequires; the local
# toolchain (meson/gtk4 + the in-tree blueprint-compiler subproject) is enough.
rpm: dist-tarball
	@$(LOAD_ENV) rpmbuild --define "_topdir $(RPM_TOP)" \
	  $${CLOUDY_MS_CLIENT_ID:+--define "ms_client_id $$CLOUDY_MS_CLIENT_ID"} \
	  $${CLOUDY_GOOGLE_CLIENT_ID:+--define "google_client_id $$CLOUDY_GOOGLE_CLIENT_ID"} \
	  $${CLOUDY_GOOGLE_CLIENT_SECRET:+--define "google_client_secret $$CLOUDY_GOOGLE_CLIENT_SECRET"} \
	  --nodeps -ba $(SPEC)
	@echo; echo "RPMs:"; find $(RPM_TOP)/RPMS $(RPM_TOP)/SRPMS -name '*.rpm'

## Source RPM only.
srpm: dist-tarball
	@$(LOAD_ENV) rpmbuild --define "_topdir $(RPM_TOP)" --nodeps -bs $(SPEC)
	@find $(RPM_TOP)/SRPMS -name '*.rpm'

## --- Flatpak (local test build; never published) -------------------------
# flatpak-builder invocation. Default: the sandboxed org.flatpak.Builder flatpak
# (no host install needed; --env passes git config into the sandbox). Override
# with the native binary for CI / root contexts where the sandboxed app can't
# create its state dir:  make FLATPAK_BUILDER=flatpak-builder flatpak-bundle
FLATPAK_BUILDER ?= flatpak run \
  --env=GIT_CONFIG_COUNT=1 \
  --env=GIT_CONFIG_KEY_0=safe.bareRepository \
  --env=GIT_CONFIG_VALUE_0=all \
  org.flatpak.Builder

## Build + install to the user installation, baking creds from .env into a
## LOCAL manifest under _build/ (the committed manifest stays credential-free).
flatpak: flatpak-test
flatpak-test:
	@mkdir -p $(FLATPAK_DIR)
	@$(LOAD_ENV) python3 scripts/flatpak-local-manifest.py \
	  $(APP_ID).yml $(CURDIR) $(FLATPAK_DIR)/$(APP_ID).local.yml
	$(FLATPAK_BUILDER) --user --install --force-clean \
	  --install-deps-from=flathub \
	  $(FLATPAK_DIR)/build $(FLATPAK_DIR)/$(APP_ID).local.yml
	@echo "Installed (user). Run it with: make flatpak-run"

## Run the installed Flatpak
flatpak-run:
	flatpak run $(APP_ID)

## Export a single-file, installable .flatpak bundle (carries the app + icon).
flatpak-bundle:
	@mkdir -p $(FLATPAK_DIR) $(RELEASE_DIR)
	@$(LOAD_ENV) python3 scripts/flatpak-local-manifest.py \
	  $(APP_ID).yml $(CURDIR) $(FLATPAK_DIR)/$(APP_ID).local.yml
	# Build and export into a local OSTree repo (no system install).
	$(FLATPAK_BUILDER) --force-clean --install-deps-from=flathub \
	  --repo=$(FLATPAK_REPO) \
	  $(FLATPAK_DIR)/build $(FLATPAK_DIR)/$(APP_ID).local.yml
	# Bundle the repo into one file; --runtime-repo tells installers where to
	# fetch the GNOME runtime from (Flathub).
	flatpak build-bundle \
	  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
	  $(FLATPAK_REPO) $(FLATPAK_BUNDLE) $(APP_ID)
	@echo "Flatpak bundle: $(FLATPAK_BUNDLE)"

## Collect distributable artifacts (RPM + .flatpak bundle) into release/.
release: rpm flatpak-bundle
	@mkdir -p $(RELEASE_DIR)
	@cp $(RPM_TOP)/RPMS/noarch/*.rpm $(RELEASE_DIR)/
	# Install the freshly built bundle so the *running* app matches the release
	# (otherwise `make release` leaves the previously-installed build running).
	@flatpak install --user --noninteractive --reinstall "$(FLATPAK_BUNDLE)" \
	  || flatpak install --user --noninteractive "$(FLATPAK_BUNDLE)" || true
	@echo; echo "=== release/ ==="; ls -lh $(RELEASE_DIR)
	@echo
	@echo "Installed the bundle to the user installation (running app == release)."
	@echo "Share/install elsewhere:"
	@echo "  RPM:     sudo dnf install ./$(RELEASE_DIR)/cloudy-*.rpm"
	@echo "  Flatpak: flatpak install --user ./$(FLATPAK_BUNDLE)"

## Install the host-side Nautilus extension (runs outside the sandbox)
install-nautilus:
	mkdir -p "$(NAUTILUS_EXT_DIR)"
	cp nautilus-extension/cloudy_nautilus.py "$(NAUTILUS_EXT_DIR)/"
	-nautilus -q
	@echo "Installed. Nautilus will reload the extension on next start."

## Remove the host-side Nautilus extension
uninstall-nautilus:
	rm -f "$(NAUTILUS_EXT_DIR)/cloudy_nautilus.py"
	rm -rf "$(NAUTILUS_EXT_DIR)/__pycache__"
	-nautilus -q

## Lint the Python sources
lint:
	python3 -m py_compile $$(find src nautilus-extension -name '*.py')

## Remove build artifacts
clean:
	rm -rf $(BUILDDIR) $(FLATPAK_DIR) .flatpak-builder
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

## clean + remove the local install prefix
distclean: clean
	rm -rf $(PREFIX)
