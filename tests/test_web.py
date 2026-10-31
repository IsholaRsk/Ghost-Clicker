import base64
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import web


class WebUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.gateway_path = root / "runtime" / "proxy-gateway.txt"
        self.settings_path = root / "runtime" / "settings.json"
        self.reports_path = root / "runtime" / "reports.json"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ADMIN_TOKEN": "TEST_ADMIN_TOKEN",
                "PROXY_GATEWAY_FILE": str(self.gateway_path),
                "SETTINGS_FILE": str(self.settings_path),
                "REPORTS_FILE": str(self.reports_path),
            },
        )
        self.environment.start()
        self.client = web.APP.test_client()
        credentials = base64.b64encode(b"admin:TEST_ADMIN_TOKEN").decode("ascii")
        self.headers = {"Authorization": f"Basic {credentials}"}
        with web.STATE_LOCK:
            web.RUN_STATE.update(
                running=False,
                source="",
                started_at="",
                finished_at="",
                last_error="",
                next_run_at="",
            )

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_web_uses_the_shared_ephemeral_default_gateway_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                web.proxy_gateway_file_path(),
                Path(web.DEFAULT_PROXY_GATEWAY_FILE),
            )

    def test_home_uses_the_dark_minimalist_console_layout(self):
        response = self.client.get("/", headers=self.headers)
        content = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Console d’exécution", content)
        self.assertIn("Passerelle et cibles", content)
        self.assertIn("color-scheme:dark", content)
        self.assertIn("--canvas:#0d0f12", content)
        self.assertIn('class="status-grid"', content)
        self.assertNotIn("--canvas:#f4f4f1", content)
        self.assertNotIn("radial-gradient", content)

    def post_file(self, route, field_name, content, filename):
        return self.client.post(
            route,
            data={
                "csrf_token": web.CSRF_TOKEN,
                field_name: (io.BytesIO(content.encode("utf-8")), filename),
            },
            headers=self.headers,
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def test_gateway_upload_is_authenticated_parsed_and_stored_with_mode_0600(self):
        line = "sample-user:sample-pass@proxy.example:8000"
        response = self.client.post(
            "/gateway",
            data={"csrf_token": web.CSRF_TOKEN, "gateway": line},
            headers=self.headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["gateway"], ["1"])
        self.assertEqual(
            self.gateway_path.read_text(encoding="utf-8"), f"{line}\n"
        )
        self.assertEqual(stat.S_IMODE(self.gateway_path.stat().st_mode), 0o600)
        home = self.client.get("/", headers=self.headers)
        self.assertNotIn(line, home.get_data(as_text=True))

    def test_invalid_gateway_upload_does_not_replace_existing_secret_file(self):
        original = "sample-user:sample-pass@proxy.example:8000\n"
        self.gateway_path.parent.mkdir(parents=True)
        self.gateway_path.write_text(original, encoding="utf-8")

        response = self.client.post(
            "/gateway",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "gateway": "invalid-sensitive-content",
            },
            headers=self.headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertIn("error", query)
        self.assertEqual(self.gateway_path.read_text(encoding="utf-8"), original)
        self.assertNotIn("invalid-sensitive-content", query["error"][0])

    def test_target_upload_accepts_unrelated_public_domains_and_preserves_schedule(self):
        web.save_app_settings(
            {"targets": [], "schedule_enabled": True, "interval_minutes": 120}
        )
        content = (
            "https://first.example/page \u2060 | Continuer | Confirmer\n"
            "https://unrelated.example.org/other | Valider | Terminer\n"
        )

        response = self.post_file(
            "/upload-targets", "target_file", content, "targets.txt"
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["targets"], ["2"])
        settings = web.load_app_settings()
        self.assertEqual(
            settings["targets"],
            [
                {
                    "url": "https://first.example/page",
                    "button_text": "Continuer",
                    "second_button_text": "Confirmer",
                },
                {
                    "url": "https://unrelated.example.org/other",
                    "button_text": "Valider",
                    "second_button_text": "Terminer",
                },
            ],
        )
        self.assertTrue(settings["schedule_enabled"])
        self.assertEqual(settings["interval_minutes"], 120)

    def test_target_upload_accepts_one_label_for_a_direct_redirect(self):
        response = self.post_file(
            "/upload-targets",
            "target_file",
            "https://first.example/page | Continuer\n",
            "targets.txt",
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["targets"], ["1"])
        self.assertEqual(
            web.load_app_settings()["targets"],
            [
                {
                    "url": "https://first.example/page",
                    "button_text": "Continuer",
                    "second_button_text": "",
                }
            ],
        )
        self.assertEqual(
            web.targets_to_text(web.load_app_settings()),
            "https://first.example/page | Continuer",
        )

    def test_target_upload_still_requires_the_first_label(self):
        response = self.post_file(
            "/upload-targets",
            "target_file",
            "https://first.example/page\n",
            "targets.txt",
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertIn("error", query)
        self.assertEqual(web.load_app_settings()["targets"], [])

    def test_targets_remain_editable_online_after_file_upload(self):
        self.post_file(
            "/upload-targets",
            "target_file",
            "https://first.example/page | Continuer | Confirmer\n",
            "targets.txt",
        )

        response = self.client.post(
            "/settings",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "targets": (
                    "https://edited.example/page | Continuer | Confirmer"
                ),
                "interval_minutes": "60",
            },
            headers=self.headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        settings = web.load_app_settings()
        self.assertEqual(
            settings["targets"],
            [
                {
                    "url": "https://edited.example/page",
                    "button_text": "Continuer",
                    "second_button_text": "Confirmer",
                }
            ],
        )

    def test_csv_includes_the_random_repetition_without_ip_check_columns(self):
        web.append_report(
            {
                "source": "manuel",
                "repetition_count": 200,
                "results": [
                    {
                        "started_at": "2026-10-30T00:00:00+00:00",
                        "repetition": 7,
                        "proxy_name": "passerelle-rotative",
                        "proxy_attempts": 1,
                        "initial_url": "https://first.example/page",
                        "button_text": "Continuer",
                        "second_button_text": "Confirmer",
                        "success": True,
                    }
                ],
            }
        )

        response = self.client.get("/results.csv", headers=self.headers)
        content = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("repetition,repetitions_planifiees", content)
        self.assertIn(",7,200,passerelle-rotative,1,", content)
        self.assertNotIn("ip_sortie", content)
        self.assertNotIn("pays", content)

    def test_upload_routes_require_authentication_and_csrf(self):
        unauthorized = self.client.post("/gateway")
        self.assertEqual(unauthorized.status_code, 401)

        missing_csrf = self.client.post(
            "/gateway",
            data={"gateway": "sample-user:sample-pass@proxy.example:8000"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(missing_csrf.status_code, 303)
        query = parse_qs(urlsplit(missing_csrf.headers["Location"]).query)
        self.assertIn("error", query)
        self.assertFalse(self.gateway_path.exists())

    def test_upload_rejects_non_utf8_and_files_over_500_kib(self):
        non_utf8 = self.client.post(
            "/upload-targets",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "target_file": (io.BytesIO(b"\xff\xfe"), "targets.txt"),
            },
            headers=self.headers,
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertIn(
            "error", parse_qs(urlsplit(non_utf8.headers["Location"]).query)
        )

        oversized = self.client.post(
            "/upload-targets",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "target_file": (io.BytesIO(b"a" * 512_001), "targets.txt"),
            },
            headers=self.headers,
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertIn(
            "error", parse_qs(urlsplit(oversized.headers["Location"]).query)
        )


if __name__ == "__main__":
    unittest.main()
