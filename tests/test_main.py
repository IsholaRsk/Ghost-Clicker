import random
import tempfile
import unittest
from pathlib import Path

import main


class UrlValidationTests(unittest.TestCase):
    def test_accepts_public_https_url(self):
        self.assertEqual(
            main.validate_url("https://example.com/path?value=1"),
            "https://example.com/path?value=1",
        )

    def test_rejects_unsafe_schemes_and_private_hosts(self):
        rejected = [
            "file:///C:/Windows/System32/",
            "http://127.0.0.1/",
            "http://192.168.1.2/",
            "http://localhost/",
            "https://user:password@example.com/",
        ]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                main.validate_url(value)

    def test_domain_allowlist_accepts_subdomains(self):
        self.assertEqual(
            main.validate_url("https://www.example.com/", ("example.com",)),
            "https://www.example.com/",
        )
        with self.assertRaises(ValueError):
            main.validate_url("https://example.org/", ("example.com",))

    def test_load_links_reports_invalid_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "links.txt"
            path.write_text(
                "# commentaire\nhttps://example.com/\nhttps://example.com/\n"
                "http://127.0.0.1/\n",
                encoding="utf-8",
            )
            valid, invalid = main.load_links(path)
            self.assertEqual(valid, ["https://example.com/"])
            self.assertEqual(len(invalid), 1)
            self.assertEqual(invalid[0].line_number, 4)

    def test_loads_target_urls_from_json_or_semicolon_list(self):
        json_urls, json_invalid = main.load_links_from_environment(
            '["https://example.com/", "https://example.org/"]'
        )
        self.assertEqual(
            json_urls, ["https://example.com/", "https://example.org/"]
        )
        self.assertFalse(json_invalid)

        list_urls, list_invalid = main.load_links_from_environment(
            "https://example.com/;https://example.org/"
        )
        self.assertEqual(list_urls, json_urls)
        self.assertFalse(list_invalid)

    def test_rejects_invalid_target_urls_json(self):
        with self.assertRaises(main.ConfigurationError):
            main.load_links_from_environment('{"url": "https://example.com/"}')

    def test_navigation_guard_blocks_private_redirect(self):
        class FakeRoute:
            def __init__(self):
                self.action = ""

            def abort(self, _reason):
                self.action = "abort"

            def continue_(self):
                self.action = "continue"

        class FakeRequest:
            def __init__(self, url, frame):
                self.url = url
                self.frame = frame

            def is_navigation_request(self):
                return True

        class FakePage:
            def __init__(self):
                self.main_frame = object()
                self.handler = None

            def route(self, _pattern, handler):
                self.handler = handler

        page = FakePage()
        main.install_navigation_guard(page)
        route = FakeRoute()
        page.handler(route, FakeRequest("http://127.0.0.1/", page.main_frame))
        self.assertEqual(route.action, "abort")

        route = FakeRoute()
        page.handler(route, FakeRequest("https://example.com/", page.main_frame))
        self.assertEqual(route.action, "continue")


class ProxyConfigurationTests(unittest.TestCase):
    def test_loads_environment_references_without_storing_them_in_csv(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "proxies.txt"
            path.write_text(
                "name,country,server_env,username_env,password_env\n"
                "test-us,US,TEST_SERVER,TEST_USER,TEST_PASSWORD\n",
                encoding="utf-8",
            )
            # Le domaine .test est réservé aux tests et aucune connexion n'est effectuée.
            environment = {
                "TEST_SERVER": "http://proxy.test:8080",
                "TEST_USER": "private-user",
                "TEST_PASSWORD": "private-password",
            }
            proxies, issues = main.load_proxy_configs(path, environment)
            self.assertFalse(issues)
            self.assertEqual(len(proxies), 1)
            self.assertEqual(proxies[0].country, "US")
            self.assertEqual(proxies[0].password, "private-password")

    def test_missing_environment_variable_is_a_safe_issue(self):
        proxies, issues = main.load_proxy_configs(Path("proxies.txt"), {})
        self.assertFalse(proxies)
        self.assertEqual(len(issues), 100)
        self.assertNotIn("password", " ".join(issue.message for issue in issues).lower())

    def test_proxy_server_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            main.validate_proxy_server("http://user:password@proxy.test:8080")

    def test_selects_only_profiles_that_can_be_used_this_run(self):
        proxies = [
            main.ProxyConfig(f"proxy-{index}", "US", "http://proxy.test:8080")
            for index in range(20)
        ]
        selected = main.select_proxy_configs(
            proxies, max_proxies_per_run=10, url_count=3, rng=random.Random(42)
        )
        self.assertEqual(len(selected), 3)
        self.assertEqual(len({proxy.name for proxy in selected}), 3)
        self.assertTrue(all(proxy in proxies for proxy in selected))


class ResultTests(unittest.TestCase):
    def test_redacts_known_secret_and_url_authentication(self):
        message = main.redact_error(
            "failure private-password at http://user:password@proxy.test:8080",
            ["private-password"],
        )
        self.assertNotIn("private-password", message)
        self.assertNotIn("user:password", message)
        self.assertIn("***", message)

    def test_dashboard_escapes_result_values(self):
        rows = [
            {
                "run_id": "run-1",
                "type_enregistrement": "navigation",
                "date_heure": "2026-01-01T10:00:00+00:00",
                "proxy": "test-us",
                "url_initiale": "https://example.com/",
                "url_finale": "https://example.com/?x=<script>",
                "pays_configure": "US",
                "pays_detecte": "US",
                "adresse_ip_sortie": "203.0.113.1",
                "statut_http": "200",
                "resultat_test": "SUCCES",
                "duree_ms": "123",
                "message_erreur": "<script>alert(1)</script>",
            }
        ]
        dashboard = main.build_dashboard(rows, 100)
        self.assertIn("100.0%", dashboard)
        self.assertNotIn("<script>alert(1)</script>", dashboard)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", dashboard)


if __name__ == "__main__":
    unittest.main()
