# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
#
# Convenience wrapper around Meson / Flatpak for reproducible builds.
# See docs/BUILDING.md for details.

APP_ID      := com.fiberelements.Cloudy
BUILDDIR    := _build
PREFIX      := $(CURDIR)/_install
FLATPAK_DIR := _build/flatpak

SCHEMA_DIR  := $(PREFIX)/share/glib-2.0/schemas

NAUTILUS_EXT_DIR := $(HOME)/.local/share/nautilus-python/extensions

.PHONY: all bootstrap setup build install run clean distclean \
        flatpak flatpak-run lint test install-nautilus uninstall-nautilus

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

## Run the Meson test suite (schema/desktop/metainfo validation)
test: build
	meson test -C $(BUILDDIR) --print-errorlogs

## Build + install the Flatpak (reproducible, pinned runtime)
flatpak:
	flatpak-builder --user --install --force-clean $(FLATPAK_DIR) $(APP_ID).yml

## Run the installed Flatpak
flatpak-run:
	flatpak run $(APP_ID)

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
