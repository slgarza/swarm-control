import unittest

from demo.numbers import clamp


class ClampTests(unittest.TestCase):
    def test_clamps_above_upper_bound(self):
        self.assertEqual(clamp(15, 0, 10), 10)
