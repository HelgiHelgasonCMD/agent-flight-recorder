import unittest

import app


class ConfigTest(unittest.TestCase):
    def test_loads(self):
        self.assertEqual(app.load()["currency"], "EUR")
