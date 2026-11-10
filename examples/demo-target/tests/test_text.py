import unittest

from demo.text import normalize_name


class NameTests(unittest.TestCase):
    def test_preserves_simple_name(self):
        self.assertEqual(normalize_name("Ana"), "Ana")
