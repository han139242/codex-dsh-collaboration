import unittest

from names import unique_names


class UniqueNamesTest(unittest.TestCase):
    def test_trim_and_keep_first_spelling(self):
        self.assertEqual(unique_names(["  Ada ", "ADA", "Grace "]), ["Ada", "Grace"])

    def test_skip_blank_names(self):
        self.assertEqual(unique_names([" ", "\t", "Lin"]), ["Lin"])

    def test_unicode_casefold(self):
        self.assertEqual(unique_names(["Straße", "STRASSE"]), ["Straße"])

    def test_canonically_equivalent_accents(self):
        first = "Cafe\u0301"
        self.assertEqual(unique_names([first, "Caf\u00e9"]), [first])

    def test_keep_compatibility_distinction(self):
        self.assertEqual(unique_names(["Ａ", "A"]), ["Ａ", "A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
