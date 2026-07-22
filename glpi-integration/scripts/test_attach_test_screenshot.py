import os
import unittest

os.environ.setdefault("GLPI_API_URL", "http://testserver/api.php/v2.3")
os.environ.setdefault("GLPI_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GLPI_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GLPI_USER", "test-user")
os.environ.setdefault("GLPI_PASSWORD", "test-password")

import attach_test_screenshot as a


class TestGenerateTestImage(unittest.TestCase):
    def test_returns_valid_png_bytes(self):
        data = a.generate_test_image()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_image_is_reasonably_sized(self):
        data = a.generate_test_image()
        self.assertGreater(len(data), 100)


if __name__ == "__main__":
    unittest.main()
