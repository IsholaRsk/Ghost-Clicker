import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main


def make_settings(proxies=None):
    return main.BrowserSettings(
        proxies=proxies
        or (
            main.ProxyConfig(
                "proxy-001",
                "http://proxy-one.example:8000",
                "SENSITIVE_USER_MARKER",
                "SENSITIVE_PASSWORD_MARKER",
            ),
            main.ProxyConfig(
                "proxy-002",
                "http://proxy-two.example:8001",
                "SECOND_USER_MARKER",
                "SECOND_PASSWORD_MARKER",
            ),
        ),
        ip_check_url="https://ipinfo.io/json",
        headless=True,
        navigation_timeout_ms=30_000,
        click_timeout_ms=15_000,
        redirect_timeout_ms=30_000,
        wait_after_redirect_seconds=0,
        delay_between_targets_seconds=0,
    )


class ValidationTests(unittest.TestCase):
    def test_accepts_public_urls_from_unrelated_domains(self):
        for value in (
            "https://www.example.com/page",
            "https://unrelated.example.org/other",
        ):
            with self.subTest(value=value):
                self.assertEqual(main.validate_public_url(value), value)

    def test_rejects_private_local_or_unsafe_urls(self):
        values = [
            "http://127.0.0.1/",
            "http://192.168.1.1/",
            "http://localhost/",
            "file:///tmp/test",
            "https://embedded-user:embedded-pass@example.com/",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(main.ConfigurationError):
                main.validate_public_url(value)

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
            main.validate_proxy_server(
                "http://embedded-user:embedded-pass@proxy.test:8000"
            )


class ProxyFileTests(unittest.TestCase):
    def proxy_line(self, suffix, port):
        return f"sample-user-{suffix}:sample-pass-{suffix}@proxy-{suffix}.example:{port}"

    def test_parses_rayobyte_lines_and_ignores_exact_duplicates(self):
        first = self.proxy_line("one", 8000)
        second = self.proxy_line("two", 8001)
        proxies = main.parse_proxy_list_text(
            f"# export\n{first}\n\n{first}\n{second}\n"
        )

        self.assertEqual(len(proxies), 2)
        self.assertEqual(proxies[0].name, "proxy-001")
        self.assertEqual(proxies[0].server, "http://proxy-one.example:8000")
        self.assertEqual(proxies[0].username, "sample-user-one")
        self.assertEqual(proxies[0].password, "sample-pass-one")
        self.assertEqual(proxies[1].name, "proxy-002")

    def test_rejects_invalid_or_empty_proxy_files_without_echoing_the_line(self):
        invalid = "SENSITIVE_INVALID_LINE"
        with self.assertRaises(main.ConfigurationError) as raised:
            main.parse_proxy_list_text(invalid)
        self.assertNotIn(invalid, str(raised.exception))

        with self.assertRaises(main.ConfigurationError):
            main.parse_proxy_list_text("# commentaire uniquement\n")

    def test_limits_file_to_one_hundred_unique_proxies(self):
        content = "\n".join(
            self.proxy_line(str(index), 8000 + index) for index in range(101)
        )
        with self.assertRaises(main.ConfigurationError):
            main.parse_proxy_list_text(content)

    def test_loads_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "proxies.txt"
            path.write_text(
                "\ufeff" + self.proxy_line("one", 8000), encoding="utf-8"
            )
            proxies = main.load_proxy_file(path)
        self.assertEqual(len(proxies), 1)

    def test_missing_file_has_an_upload_instruction(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.txt"
            with self.assertRaises(main.ConfigurationError) as raised:
                main.load_proxy_file(missing)
        self.assertIn("Téléversez", str(raised.exception))


class SecurityTests(unittest.TestCase):
    def test_redacts_all_proxy_credentials(self):
        settings = make_settings()
        message = main.redact_error(
            "SENSITIVE_USER_MARKER:SENSITIVE_PASSWORD_MARKER "
            "SECOND_USER_MARKER SECOND_PASSWORD_MARKER at "
            "http://SENSITIVE_USER_MARKER:SENSITIVE_PASSWORD_MARKER@proxy.test:8000",
            settings,
        )
        for secret in (
            "SENSITIVE_USER_MARKER",
            "SENSITIVE_PASSWORD_MARKER",
            "SECOND_USER_MARKER",
            "SECOND_PASSWORD_MARKER",
        ):
            self.assertNotIn(secret, message)
        self.assertIn("***", message)

    def test_playwright_proxy_configuration(self):
        self.assertEqual(
            make_settings().proxies[0].playwright_proxy(),
            {
                "server": "http://proxy-one.example:8000",
                "username": "SENSITIVE_USER_MARKER",
                "password": "SENSITIVE_PASSWORD_MARKER",
            },
        )

    def test_extracts_ip_information(self):
        self.assertEqual(
            main.extract_ip_information({"ip": "203.0.113.10", "country": "US"}),
            ("203.0.113.10", "US"),
        )

    def test_request_guard_allows_public_hosts_and_blocks_private_hosts(self):
        class FakeRoute:
            def __init__(self):
                self.action = ""

            def continue_(self):
                self.action = "continue"

            def abort(self, reason):
                self.action = reason

        class FakePage:
            def route(self, _pattern, callback):
                self.callback = callback

        class FakeRequest:
            def __init__(self, url):
                self.url = url

        page = FakePage()
        main.install_request_guard(page)

        public_route = FakeRoute()
        page.callback(public_route, FakeRequest("https://different.example.org/page"))
        self.assertEqual(public_route.action, "continue")

        private_route = FakeRoute()
        page.callback(private_route, FakeRequest("http://127.0.0.1/private"))
        self.assertEqual(private_route.action, "blockedbyclient")


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

        result = main.VisitResult(
            started_at="now",
            initial_url="https://example.com/",
            button_text="Continuer",
            second_button_text="Confirmer",
        )
        browser = FakeBrowser()
        context, _page = main.open_verified_proxy_context(
            browser, make_settings(), result, random.Random(4)
        )
        self.assertEqual(result.proxy_attempts, 2)
        self.assertEqual(result.exit_ip, "203.0.113.20")
        self.assertTrue(browser.contexts[0].closed)
        self.assertIs(context, browser.contexts[1])

    def test_stops_after_two_failed_proxy_checks(self):
        class FakeResponse:
            status = 503

        class FakePage:
            def goto(self, *_args, **_kwargs):
                return FakeResponse()

        class FakeContext:
            def __init__(self):
                self.closed = False

            def set_default_navigation_timeout(self, _timeout):
                return None

            def new_page(self):
                return FakePage()

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.contexts = []

            def new_context(self, **_kwargs):
                context = FakeContext()
                self.contexts.append(context)
                return context

        result = main.VisitResult(
            "now", "https://example.com/", "Continuer", "Confirmer"
        )
        browser = FakeBrowser()
        with self.assertRaises(RuntimeError):
            main.open_verified_proxy_context(
                browser, make_settings(), result, random.Random(4)
            )
        self.assertEqual(result.proxy_attempts, 2)
        self.assertTrue(all(context.closed for context in browser.contexts))


class BrowserLifecycleTests(unittest.TestCase):
    def test_launches_and_fully_closes_a_browser_for_each_target(self):
        class FakeResponse:
            status = 200

            def __init__(self, ip_check=False):
                self.ip_check = ip_check

            def json(self):
                return {"ip": "203.0.113.20", "country": "US"}

            def text(self):
                return "{}"

        class FakeButton:
            def __init__(self, page, name):
                self.page = page
                self.name = name
                self.first = self

            def wait_for(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.page.clicked_labels.append(self.name)
                if len(self.page.clicked_labels) == 2:
                    self.page.url += "/redirected"

        class FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.clicked_labels = []
                self.role_requests = []
                self.main_frame = object()

            def route(self, _pattern, callback):
                self.route_callback = callback

            def goto(self, url, **_kwargs):
                if url == "https://ipinfo.io/json":
                    return FakeResponse(ip_check=True)
                self.url = url
                return FakeResponse()

            def on(self, *_args):
                return None

            def remove_listener(self, *_args):
                return None

            def get_by_role(self, role, name, exact):
                self.role_requests.append((role, name, exact))
                return FakeButton(self, name)

            def wait_for_load_state(self, *_args, **_kwargs):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()
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
                self.closed = False

            def new_context(self, **_kwargs):
                context = FakeContext()
                self.contexts.append(context)
                return context

            def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self):
                self.browsers = []

            def launch(self, **_kwargs):
                browser = FakeBrowser()
                self.browsers.append(browser)
                return browser

        class FakePlaywrightManager:
            def __init__(self, chromium):
                self.playwright = type("FakePlaywright", (), {"chromium": chromium})()

            def __enter__(self):
                return self.playwright

            def __exit__(self, *_args):
                return None

        chromium = FakeChromium()
        manager = FakePlaywrightManager(chromium)
        settings = make_settings(
            proxies=(
                main.ProxyConfig(
                    "proxy-001",
                    "http://proxy-one.example:8000",
                    "SENSITIVE_USER_MARKER",
                    "SENSITIVE_PASSWORD_MARKER",
                ),
            )
        )
        targets = [
            main.Target("https://one.example/page", "Continuer", "Confirmer"),
            main.Target("https://two.example/page", "Valider", "Terminer"),
        ]

        with mock.patch("playwright.sync_api.sync_playwright", return_value=manager):
            report = main.run_targets(settings, targets, random.Random(2))

        self.assertTrue(report.success)
        self.assertEqual(len(chromium.browsers), 2)
        self.assertTrue(all(browser.closed for browser in chromium.browsers))
        for browser, target in zip(chromium.browsers, targets, strict=True):
            self.assertEqual(len(browser.contexts), 1)
            self.assertTrue(browser.contexts[0].closed)
            page = browser.contexts[0].page
            self.assertEqual(
                page.clicked_labels,
                [target.button_text, target.second_button_text],
            )
            self.assertEqual(
                page.role_requests,
                [
                    ("button", target.button_text, True),
                    ("button", target.second_button_text, True),
                ],
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
