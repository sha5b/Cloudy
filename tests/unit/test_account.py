# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Account model: dict round-trip + personal/business classification."""

import unittest

from cloudy.core.account_registry import Account


def _acct(**kw):
    base = dict(id="a1", display_name="x@example.com", provider="microsoft",
                module_id="microsoft365")
    base.update(kw)
    return Account(**base)


class TestAccount(unittest.TestCase):
    def test_from_dict_tolerates_missing_optional_keys(self):
        a = Account.from_dict({"id": "a1", "display_name": "n",
                               "provider": "google", "module_id": "gmail"})
        self.assertFalse(a.signed_in)
        self.assertEqual(a.shared_mailboxes, [])
        self.assertEqual(a.pinned_sources, [])
        self.assertEqual(a.muted_sources, [])

    def test_to_dict_from_dict_roundtrip(self):
        a = _acct(signed_in=True, shared_mailboxes=["s@x.com"],
                  pinned_sources=[{"kind": "mail", "source": "shared",
                                   "id": "s@x.com", "name": "S"}],
                  muted_sources=[{"kind": "chat", "id": "c1"}])
        b = Account.from_dict(a.to_dict())
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_from_dict_copies_lists(self):
        src = {"id": "a", "display_name": "n", "provider": "google",
               "module_id": "gmail", "muted_sources": [{"kind": "chat", "id": "c"}]}
        a = Account.from_dict(src)
        a.muted_sources.append({"kind": "chat", "id": "c2"})
        self.assertEqual(len(src["muted_sources"]), 1)  # original untouched

    def test_is_personal_google(self):
        self.assertTrue(_acct(provider="google", display_name="me@gmail.com").is_personal)
        self.assertTrue(_acct(provider="google", display_name="me@googlemail.com").is_personal)
        self.assertFalse(_acct(provider="google", display_name="me@acme.com").is_personal)

    def test_is_personal_microsoft(self):
        for d in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
            self.assertTrue(_acct(display_name=f"me@{d}").is_personal, d)
        self.assertFalse(_acct(display_name="me@contoso.com").is_personal)

    def test_is_personal_case_insensitive(self):
        self.assertTrue(_acct(provider="google", display_name="Me@GMAIL.com").is_personal)

    def test_is_business_requires_signed_in_and_not_personal(self):
        self.assertTrue(_acct(display_name="me@contoso.com", signed_in=True).is_business)
        self.assertFalse(_acct(display_name="me@contoso.com", signed_in=False).is_business)
        self.assertFalse(_acct(display_name="me@outlook.com", signed_in=True).is_business)


if __name__ == "__main__":
    unittest.main()
