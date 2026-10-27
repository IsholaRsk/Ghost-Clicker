import unittest

import main


class ValidationTests(unittest.TestCase):
    def test_accepts_authorized_public_url(self):
        self.assertEqual(
            main.validate_public_url(
                "https://www.example.com/page", ("example.com",)
            ),
            "https://www.example.com/page",
        )

    def test_rejects_unauthorized_or_private_urls(self):
        values = [
            "https://example.org/",
            "http://127.0.0.1/",
            "http://192.168.1.1/",
            "http://localhost/",
            "file:///tmp/test",
            "https://user:password@example.com/",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(main.ConfigurationError):
                main.validate_public_url(value, ("example.com",))

    def test_requires_a_domain_allowlist(self):
        with self.assertRaises(main.ConfigurationError):
            main.parse_allowed_domains("")
        self.assertEqual(
            main.parse_allowed_domains("example.com,www.example.org"),
            ("example.com", "www.example.org"),
        )

    def test_validates_button_text(self):
        self.assertEqual(main.validate_button_text(" Continuer "), "Continuer")
        with self.assertRaises(main.ConfigurationError):
            main.validate_button_text("")

    def test_validates_proxy_without_embedded_credentials(self):
        self.assertEqual(
            main.validate_proxy_server("http://proxy.test:8000/"),
            "http://proxy.test:8000",
        )
        with self.assertRaises(main.ConfigurationError):
            main.validate_proxy_server("http://user:password@proxy.test:8000")


class SecurityTests(unittest.TestCase):
    def make_settings(self):
        return main.BrowserSettings(
            proxy_server="http://proxy.test:8000",
            proxy_username="private-user",
            proxy_password="private-password",
            ip_check_url="https://ipinfo.io/json",
            allowed_domains=("example.com",),
            headless=True,
            navigation_timeout_ms=30_000,
            click_timeout_ms=15_000,
            redirect_timeout_ms=30_000,
            wait_after_redirect_seconds=0,
            delay_between_targets_seconds=0,
        )

    def test_redacts_proxy_credentials(self):
        message = main.redact_error(
            "private-user:private-password at "
            "http://private-user:private-password@proxy.test:8000",
            self.make_settings(),
        )
        self.assertNotIn("private-user", message)
        self.assertNotIn("private-password", message)
        self.assertIn("***", message)

    def test_playwright_proxy_configuration(self):
        self.assertEqual(
            self.make_settings().playwright_proxy(),
            {
                "server": "http://proxy.test:8000",
                "username": "private-user",
                "password": "private-password",
            },
        )

    def test_extracts_ip_information(self):
        self.assertEqual(
            main.extract_ip_information({"ip": "203.0.113.10", "country": "US"}),
            ("203.0.113.10", "US"),
        )


class RedirectTests(unittest.TestCase):
    def test_waits_until_url_changes(self):
        class FakePage:
            def __init__(self):
                self.url = "https://example.com/start"
                self.calls = 0

            def wait_for_timeout(self, _milliseconds):
                self.calls += 1
                if self.calls == 2:
                    self.url = "https://example.com/final"

            def wait_for_load_state(self, _state, timeout):
                self.timeout = timeout

        page = FakePage()
        main.wait_for_redirect(page, page.url, 2_000)
        self.assertEqual(page.url, "https://example.com/final")


if __name__ == "__main__":
    unittest.main()
