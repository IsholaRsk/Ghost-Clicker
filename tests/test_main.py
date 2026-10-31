import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main


def make_settings(gateway=None):
    return main.BrowserSettings(
        gateway=gateway
        or main.ProxyConfig(
            "passerelle-rotative",
            "http://proxy-gateway.example:8000",
            "SENSITIVE_USER_MARKER",
            "SENSITIVE_PASSWORD_MARKER",
        ),
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

    def test_removes_invisible_unicode_noise_at_url_boundaries(self):
        dirty = " \u2060https://www.example.com/page \u2060 "
        self.assertEqual(
            main.validate_public_url(dirty),
            "https://www.example.com/page",
        )

    def test_rejects_spaces_or_invisible_characters_inside_urls(self):
        for value in (
            "https://www.example.com/bad path",
            "https://www.example.com/bad\u2060path",
        ):
            with self.subTest(value=value), self.assertRaises(main.ConfigurationError):
                main.validate_public_url(value)

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
        self.assertEqual(main.validate_optional_button_text(""), "")
        self.assertEqual(
            main.validate_optional_button_text(" Confirmer "), "Confirmer"
        )
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


class ProxyGatewayFileTests(unittest.TestCase):
    def gateway_line(self, suffix="gateway", port=8000):
        return (
            f"sample-user-{suffix}:sample-pass-{suffix}"
            f"@proxy-{suffix}.example:{port}"
        )

    def test_engine_uses_the_ephemeral_default_gateway_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                main.proxy_gateway_path(Path("/app")),
                Path(main.DEFAULT_PROXY_GATEWAY_FILE),
            )

    def test_relative_gateway_path_remains_relative_to_the_application(self):
        with mock.patch.dict(
            os.environ, {"PROXY_GATEWAY_FILE": "proxy-gateway.txt"}, clear=True
        ):
            self.assertEqual(
                main.proxy_gateway_path(Path("/app")),
                Path("/app/proxy-gateway.txt"),
            )

    def test_parses_one_rotating_gateway(self):
        gateway = main.parse_proxy_gateway_text(
            f"# passerelle\n{self.gateway_line()}\n"
        )

        self.assertEqual(gateway.name, "passerelle-rotative")
        self.assertEqual(gateway.server, "http://proxy-gateway.example:8000")
        self.assertEqual(gateway.username, "sample-user-gateway")
        self.assertEqual(gateway.password, "sample-pass-gateway")

    def test_rejects_a_list_with_more_than_one_gateway(self):
        content = (
            f"{self.gateway_line('one', 8000)}\n"
            f"{self.gateway_line('two', 8001)}\n"
        )
        with self.assertRaises(main.ConfigurationError) as raised:
            main.parse_proxy_gateway_text(content)
        self.assertIn("une seule passerelle", str(raised.exception))

    def test_rejects_invalid_or_empty_gateway_without_echoing_the_line(self):
        invalid = "SENSITIVE_INVALID_LINE"
        with self.assertRaises(main.ConfigurationError) as raised:
            main.parse_proxy_gateway_text(invalid)
        self.assertNotIn(invalid, str(raised.exception))

        with self.assertRaises(main.ConfigurationError):
            main.parse_proxy_gateway_text("# commentaire uniquement\n")

    def test_loads_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "proxy-gateway.txt"
            path.write_text(
                "\ufeff" + self.gateway_line(), encoding="utf-8"
            )
            gateway = main.load_proxy_gateway(path)
        self.assertEqual(gateway.name, "passerelle-rotative")

    def test_missing_file_has_an_upload_instruction(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.txt"
            with self.assertRaises(main.ConfigurationError) as raised:
                main.load_proxy_gateway(missing)
        self.assertIn("Enregistrez", str(raised.exception))


class SecurityTests(unittest.TestCase):
    def test_redacts_all_proxy_credentials(self):
        settings = make_settings()
        message = main.redact_error(
            "SENSITIVE_USER_MARKER:SENSITIVE_PASSWORD_MARKER at "
            "http://SENSITIVE_USER_MARKER:SENSITIVE_PASSWORD_MARKER@proxy.test:8000",
            settings,
        )
        for secret in (
            "SENSITIVE_USER_MARKER",
            "SENSITIVE_PASSWORD_MARKER",
        ):
            self.assertNotIn(secret, message)
        self.assertIn("***", message)

    def test_playwright_proxy_configuration(self):
        self.assertEqual(
            make_settings().gateway.playwright_proxy(),
            {
                "server": "http://proxy-gateway.example:8000",
                "username": "SENSITIVE_USER_MARKER",
                "password": "SENSITIVE_PASSWORD_MARKER",
            },
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


class ProxyGatewayConnectionTests(unittest.TestCase):
    def test_opens_one_context_without_navigating_to_an_ip_check(self):
        class FakePage:
            def __init__(self):
                self.goto_calls = []

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))

        class FakeContext:
            def __init__(self):
                self.page = FakePage()
                self.timeout = None

            def set_default_navigation_timeout(self, timeout):
                self.timeout = timeout

            def new_page(self):
                return self.page

        class FakeBrowser:
            def __init__(self):
                self.contexts = []
                self.proxy_configurations = []

            def new_context(self, **kwargs):
                self.proxy_configurations.append(kwargs["proxy"])
                context = FakeContext()
                self.contexts.append(context)
                return context

        result = main.VisitResult(
            started_at="now",
            initial_url="https://example.com/",
            button_text="Continuer",
            second_button_text="Confirmer",
        )
        browser = FakeBrowser()
        context, page = main.open_proxy_context(browser, make_settings(), result)

        self.assertIs(context, browser.contexts[0])
        self.assertIs(page, context.page)
        self.assertEqual(len(browser.contexts), 1)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(result.proxy_attempts, 1)
        self.assertEqual(
            browser.proxy_configurations,
            [make_settings().gateway.playwright_proxy()],
        )


class BrowserLifecycleTests(unittest.TestCase):
    def test_launches_and_fully_closes_a_browser_for_each_target(self):
        class FakeResponse:
            status = 200

        class FakeButton:
            def __init__(self, page, name, changed_name):
                self.page = page
                self.name = name
                self.changed_name = changed_name
                self.first = self

            def wait_for(self, **_kwargs):
                return None

            def bounding_box(self):
                return {"x": 100, "y": 200, "width": 20, "height": 40}

            def click(self, **_kwargs):
                self.page.clicked_labels.append(self.name)
                self.page.screen_text = self.changed_name
                if self.name == "Entrer":
                    self.page.url += "/redirected"

        class FakeMouse:
            def __init__(self, page):
                self.page = page

            def click(self, x, y):
                self.page.coordinate_clicks.append((x, y))
                self.page.clicked_labels.append(self.page.screen_text)
                self.page.url += "/redirected"

        class FakePage:
            def __init__(self):
                self.changed_labels = {
                    "Continuer": "Continue in source language",
                    "Valider": "Finish in source language",
                    "Entrer": "Direct redirect",
                }
                self.url = "about:blank"
                self.clicked_labels = []
                self.text_requests = []
                self.coordinate_clicks = []
                self.waits = []
                self.goto_calls = []
                self.screen_text = ""
                self.mouse = FakeMouse(self)
                self.main_frame = object()

            def route(self, _pattern, callback):
                self.route_callback = callback

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                self.url = url
                return FakeResponse()

            def on(self, *_args):
                return None

            def remove_listener(self, *_args):
                return None

            def get_by_text(self, text, exact):
                self.text_requests.append((text, exact))
                self.screen_text = text
                return FakeButton(self, text, self.changed_labels[text])

            def wait_for_load_state(self, *_args, **_kwargs):
                return None

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

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
        settings = make_settings()
        targets = [
            main.Target("https://one.example/page", "Continuer", "Confirmer"),
            main.Target("https://two.example/page", "Valider", "Terminer"),
            main.Target("https://direct.example/page", "Entrer"),
        ]

        with mock.patch(
            "playwright.sync_api.sync_playwright", return_value=manager
        ):
            report = main.run_targets(
                settings,
                targets,
                repetition_count=2,
                execution_mode="fixed",
            )

        self.assertTrue(report.success)
        self.assertEqual(report.repetition_count, 2)
        self.assertEqual(report.completed_repetitions, 2)
        self.assertEqual(report.completed_visits, 6)
        self.assertEqual(
            [result.repetition for result in report.results],
            [1, 1, 1, 2, 2, 2],
        )
        self.assertEqual(len(chromium.browsers), 6)
        self.assertTrue(all(browser.closed for browser in chromium.browsers))
        expected_targets = targets * 2
        for browser, target in zip(
            chromium.browsers, expected_targets, strict=True
        ):
            self.assertEqual(len(browser.contexts), 1)
            self.assertTrue(browser.contexts[0].closed)
            page = browser.contexts[0].page
            self.assertEqual(page.goto_calls, [target.url])
            self.assertNotIn("https://ipinfo.io/json", page.goto_calls)
            expected_clicks = [target.button_text]
            if target.second_button_text:
                expected_clicks.append(page.changed_labels[target.button_text])
            self.assertEqual(page.clicked_labels, expected_clicks)
            self.assertEqual(
                page.text_requests,
                [(target.button_text, True)],
            )
            expected_coordinates = (
                [(110, 220)] if target.second_button_text else []
            )
            expected_waits = [500] if target.second_button_text else []
            self.assertEqual(page.coordinate_clicks, expected_coordinates)
            self.assertEqual(page.waits, expected_waits)


class RepetitionPlanTests(unittest.TestCase):
    def test_resolves_fixed_range_and_continuous_modes(self):
        self.assertEqual(main.resolve_repetition_count("fixed", 12, 2, 8), 12)
        with mock.patch("main.random.randint", return_value=6) as choose:
            self.assertEqual(
                main.resolve_repetition_count("range", 12, 2, 8), 6
            )
        choose.assert_called_once_with(2, 8)
        self.assertIsNone(
            main.resolve_repetition_count("continuous", 12, 2, 8)
        )

    def test_rejects_invalid_repetition_plans(self):
        invalid_plans = [
            ("unknown", 1, 1, 2),
            ("fixed", 0, 1, 2),
            ("range", 1, 5, 2),
            ("fixed", main.MAX_CONFIGURED_REPETITIONS + 1, 1, 2),
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan), self.assertRaises(
                main.ConfigurationError
            ):
                main.resolve_repetition_count(*plan)

    def test_continuous_mode_runs_until_stop_is_requested(self):
        class FakePlaywrightManager:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                return None

        stop = {"requested": False}

        def fake_visit(_playwright, _settings, target, repetition):
            return main.VisitResult(
                started_at="now",
                initial_url=target.url,
                button_text=target.button_text,
                second_button_text=target.second_button_text,
                repetition=repetition,
                success=True,
            )

        expected_visits = main.MAX_CONTINUOUS_RESULTS + 2

        def update_progress(report):
            if report.completed_visits == expected_visits:
                stop["requested"] = True

        with (
            mock.patch(
                "playwright.sync_api.sync_playwright",
                return_value=FakePlaywrightManager(),
            ),
            mock.patch("main.visit_target", side_effect=fake_visit),
        ):
            report = main.run_targets(
                make_settings(),
                [main.Target("https://example.com", "Entrer")],
                repetition_count=None,
                execution_mode="continuous",
                should_stop=lambda: stop["requested"],
                on_progress=update_progress,
            )

        self.assertTrue(report.stopped)
        self.assertTrue(report.success)
        self.assertEqual(report.execution_mode, "continuous")
        self.assertIsNone(report.repetition_count)
        self.assertEqual(report.completed_repetitions, expected_visits)
        self.assertEqual(report.completed_visits, expected_visits)
        self.assertEqual(len(report.results), main.MAX_CONTINUOUS_RESULTS)
        self.assertEqual(report.discarded_results, 2)


class TargetResponseTests(unittest.TestCase):
    def test_accepts_success_and_redirect_statuses(self):
        for status in (200, 302):
            with self.subTest(status=status):
                response = type("FakeResponse", (), {"status": status})()
                main.require_successful_target_response(response)

    def test_reports_missing_or_failed_target_responses(self):
        with self.assertRaises(RuntimeError):
            main.require_successful_target_response(None)

        response = type("FakeResponse", (), {"status": 404})()
        with self.assertRaisesRegex(RuntimeError, "404"):
            main.require_successful_target_response(response)


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
