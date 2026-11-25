"""owners_std placeholders: source strings that are not owners get a bracketed owner_norm (so
rankings and the entity layer exclude them); real names that merely resemble them are untouched.
Offline: runs normalize_series on in-memory strings, no alias file, no parcel data."""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import owners_std  # noqa: E402


def norm(*names):
    return list(owners_std.normalize_series(pd.Series(list(names), dtype=object), {}))


class PlaceholderTest(unittest.TestCase):
    def test_exact_placeholders(self):
        self.assertEqual(norm("INFORMATION ONLY", "Information Only", "STREET"),
                         ["[INFORMATION ONLY - source placeholder]"] * 2
                         + ["[STREET - source placeholder]"])

    def test_pointers_to_another_parcel(self):
        got = norm("LISTED WITH 36123016", "LISTED WITH 10126401 TO 403", "LISTED WITH",
                   "SEE 03 001 280 00 FOR 2009", "SEE 02 195 001 THRU 025 FOR",
                   "SEE 01 180 009 00 FOR VALUE 2021")
        self.assertEqual(got[:3], ["[LISTED WITH ANOTHER PARCEL - source placeholder]"] * 3)
        self.assertEqual(got[3:], ["[SEE ANOTHER PARCEL - source placeholder]"] * 3)

    def test_names_without_letters(self):
        self.assertEqual(norm("0", "&", "0 0 & 0", "0170001"),
                         ["[NO OWNER NAME - source placeholder]"] * 4)

    def test_real_names_untouched(self):
        self.assertEqual(norm("SEE JOHN", "SEE JOHN & MARY", "3M CO", "7 ELEVEN INC",
                              "STREET LLC", "INFORMATION ONLY SAN DIEGO UNIFIED PORT DISTRICT"),
                         ["SEE JOHN", "SEE JOHN & MARY", "3M CO", "7 ELEVEN INC", "STREET LLC",
                          "INFORMATION ONLY SAN DIEGO UNIFIED PORT DISTRICT"])

    def test_missing_owner_stays_missing(self):
        self.assertEqual(norm(None, "", "  "), [None, None, None])

    def test_placeholder_has_no_family(self):
        n = owners_std.normalize_series(pd.Series(["LISTED WITH 36123016", "0"], dtype=object), {})
        self.assertEqual(list(owners_std.family_series(n, [])), [None, None])


if __name__ == "__main__":
    unittest.main()
