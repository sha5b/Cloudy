# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

from __future__ import annotations


class FakeSettings:
    def __init__(self, values: dict | None = None):
        self._v = dict(values or {})

    def get_boolean(self, key):
        return bool(self._v.get(key, False))

    def get_string(self, key):
        return str(self._v.get(key, ""))

    def set_boolean(self, key, value):
        self._v[key] = bool(value)

    def set_string(self, key, value):
        self._v[key] = str(value)


class FakeRegistry:
    def __init__(self):
        self.updated = []
        self._accounts = []

    def update(self, account):
        self.updated.append(account)

    def get(self, account_id):
        return next((a for a in self._accounts if a.id == account_id), None)

    def accounts(self):
        return list(self._accounts)


class FakeApp:
    def __init__(self, settings=None, registry=None):
        self.settings = settings or FakeSettings()
        self.registry = registry or FakeRegistry()
        self.application_id = "io.github.sha5b.Cloudy"
        self.sent = []  # (notification_id, Gio.Notification)

    def send_notification(self, nid, note):
        self.sent.append((nid, note))


class FakeWindow:
    def __init__(self, app):
        self._app = app

    def get_application(self):
        return self._app
