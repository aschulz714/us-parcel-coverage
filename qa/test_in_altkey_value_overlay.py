"""IN alt-key value overlay (recon/in_altkey_value_overlay.py): a local apn whose raw rows carry
SEVERAL state parcel keys has no single value, so it is left out of the apn-keyed overlay instead of
taking the first state parcel's value (Tippecanoe 2026-09-26: 557 rows carried a sibling's value).
Offline: in-memory series only."""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "recon"))
import in_altkey_value_overlay as A  # noqa: E402


class BuildOverlayTest(unittest.TestCase):
    def test_apn_spanning_several_state_parcels_is_left_out(self):
        apn = pd.Series(["A", "A", "B", "B", "C", None])
        skey = pd.Series(["k1", "k1", "k2", "k3", "k4", "k1"])  # A: one parcel in two pieces; B: two parcels
        lut = pd.DataFrame({"assessed_value": [100, 200, 300, 400], "land_value": [10, 20, 30, 40],
                            "improvement_value": [90, 180, 270, 360]}, index=["k1", "k2", "k3", "k4"])
        ov, colliding = A.build_overlay(apn, skey, lut)
        self.assertEqual(colliding, {"B"})
        self.assertEqual(dict(zip(ov["apn"], ov["assessed_value"])), {"A": 100, "C": 400})
        self.assertEqual(set(ov["value_source"]), {"dlgf_gdb_2023"})

    def test_unmatched_key_gives_no_row(self):
        ov, colliding = A.build_overlay(pd.Series(["A"]), pd.Series(["zz"]),
                                        pd.DataFrame({c: [1] for c in A.VCOLS}, index=["k1"]))
        self.assertEqual((len(ov), colliding), (0, set()))


if __name__ == "__main__":
    unittest.main()
