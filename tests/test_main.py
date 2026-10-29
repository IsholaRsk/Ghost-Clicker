import random
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

    def test_parses_secret_proxy_list_json(self):
        proxies = main.parse_proxy_list_json(
            '[{"name":"us-1","server":"http://proxy.test:8000",'
            '"username":"user-1","password":"password-1"},'
            '{"name":"us-2","server":"http://proxy.test:8001",'
            '"username":"user-2","password":"password-2"}]'
        )
        self.assertEqual(len(proxies), 2)
        self.assertEqual(proxies[0].name, "us-1")
        self.assertEqual(proxies[1].server, "http://proxy.test:8001")

    def test_rejects_duplicate_proxy_names(self):
        with self.assertRaises(main.ConfigurationError):
            main.parse_proxy_list_json(
                '[{"name":"same","server":"http://proxy.test:8000"},'
                '{"name":"same","server":"http://proxy.test:8001"}]'
            )


class SecurityTests(unittest.TestCase):
    def make_settings(self):
        return main.BrowserSettings(
            proxies=(
                main.ProxyConfig(
                    "proxy-1",
                    "http://proxy.test:8000",
                    "private-user",
                    "private-password",
                ),
                main.ProxyConfig(
                    "proxy-2",
                    "http://proxy.test:8001",
                    "second-user",
                    "second-password",
                ),
            ),
            ip_check_url="https://ipinfo.io/json",
            allowed_domains=("example.com",),
            headless=True,
            navigation_timeout_ms=30_000,
            click_timeout_ms=15_000,
            redirect_timeout_ms=30_000,
            wait_after_redirect_seconds=0,
            delay_between_targets_seconds=0,
        )

    def test_redacts_all_proxy_credentials(self):
        message = main.redact_error(
            "private-user:private-password second-user second-password at "
            "http://private-user:private-password@proxy.test:8000",
            self.make_settings(),
        )
        for secret in (
            "private-user",
            "private-password",
            "second-user",
            "second-password",
        ):
            self.assertNotIn(secret, message)
        self.assertIn("***", message)

    def test_playwright_proxy_configuration(self):
        self.assertEqual(
            self.make_settings().proxies[0].playwright_proxy(),
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


class ProxySelectionTests(unittest.TestCase):
    def test_selects_two_distinct_candidates_at_most(self):
        proxies = [
            main.ProxyConfig(f"proxy-{index}", f"http://proxy.test:{8000 + index}")
            for index in range(5)
        ]
        selected = main.choose_proxy_candidates(proxies, random.Random(42))
        self.assertEqual(len(selected), 2)
        self.assertNotEqual(selected[0].name, selected[1].name)

    def test_retries_once_with_a_distinct_proxy_after_check_failure(self):
        class FakeResponse:
            def __init__(self, status):
                self.status = status

            def json(self):
                return {"ip": "203.0.113.20", "country": "US"}

            def text(self):
                return "{}"

        class FakePage:
            def __init__(self, status):
                self.status = status

            def goto(self, *_args, **_kwargs):
                return FakeResponse(self.status)

        class FakeContext:
            def __init__(self, status):
                self.page = FakePage(status)
                self.closed = False

            def set_default_navigation_timeout(self, _timeout):
                return None

            def new_page(self):
                return self.page

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.contexts = []

            def new_context(self, **_kwargs):
                status = 500 if not self.contexts else 200
                context = FakeContext(status)
                self.contexts.append(context)
                return context

        settings = SecurityTests().make_settings()
        result = main.VisitResult(
            started_at="now",
            initial_url="https://example.com/",
            button_text="Continuer",
        )
        browser = FakeBrowser()
        context, _page = main.open_verified_proxy_context(
            browser, settings, result, random.Random(4)
        )
        self.assertEqual(result.proxy_attempts, 2)
        self.assertEqual(result.exit_ip, "203.0.113.20")
        self.assertTrue(browser.contexts[0].closed)
        self.assertIs(context, browser.contexts[1])


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
