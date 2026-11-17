import base64
import csv
import io
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import main
import web


class WebUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.gateway_path = root / "runtime" / "proxy-gateway.txt"
        self.gateway_registry_path = root / "runtime" / "proxy-gateways.json"
        self.settings_path = root / "runtime" / "settings.json"
        self.reports_path = root / "runtime" / "reports.json"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ADMIN_TOKEN": "TEST_ADMIN_TOKEN",
                "PROXY_GATEWAY_FILE": str(self.gateway_path),
                "PROXY_GATEWAYS_FILE": str(self.gateway_registry_path),
                "SETTINGS_FILE": str(self.settings_path),
                "REPORTS_FILE": str(self.reports_path),
                "SUPABASE_URL": "",
                "SUPABASE_SECRET_KEY": "",
            },
        )
        self.environment.start()
        with web.GATEWAY_SECRETS_LOCK:
            web.GATEWAY_SECRETS_CACHE = None
        self.client = web.APP.test_client()
        credentials = base64.b64encode(b"admin:TEST_ADMIN_TOKEN").decode("ascii")
        self.headers = {"Authorization": f"Basic {credentials}"}
        self.api_headers = {
            **self.headers,
            "X-CSRF-Token": web.CSRF_TOKEN,
        }
        with web.STATE_LOCK:
            web.RUN_STATE.update(
                running=False,
                stop_requested=False,
                source="",
                started_at="",
                finished_at="",
                last_error="",
                next_run_at="",
                execution_mode="fixed",
                planned_repetitions=1,
                completed_repetitions=0,
                completed_visits=0,
                continuous_window=0,
                window_repetition_count=0,
                interval_campaign_active=False,
                interval_campaign_total=0,
                interval_campaign_launched=0,
            )
        with web.DATABASE_LOCK:
            web.DATABASE_STATE.update(
                configured=False,
                connected=False,
                last_error="",
            )
        web.STOP_EVENT.clear()

    def tearDown(self):
        with web.GATEWAY_SECRETS_LOCK:
            web.GATEWAY_SECRETS_CACHE = None
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_web_uses_the_shared_ephemeral_default_gateway_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                web.proxy_gateway_file_path(),
                Path(web.DEFAULT_PROXY_GATEWAY_FILE),
            )

    def test_home_mounts_the_dark_react_dashboard_without_secrets(self):
        response = self.client.get("/", headers=self.headers)
        content = response.get_data(as_text=True)
        javascript = (web.BASE_DIR / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (web.BASE_DIR / "static" / "app.css").read_text(
            encoding="utf-8"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('<div id="root"></div>', content)
        self.assertIn('id="ghost-clicker-bootstrap"', content)
        self.assertIn('src="/static/app.js?v=8"', content)
        self.assertIn('href="/static/app.css?v=8"', content)
        self.assertIn('name="theme-color" content="#0b0c0e"', content)
        self.assertIn('"lifetime_successful_visits":0', content)
        self.assertNotIn('http-equiv="refresh"', content)
        self.assertNotIn("sample-pass", content)

        self.assertIn("children:`Ghost`", javascript)
        self.assertIn("children:`CLICKER`", javascript)
        self.assertIn("SOLDE HEBDOMADAIRE PROJETÉ", javascript)
        self.assertIn("Solde de chaque URL", javascript)
        self.assertIn("Moyenne quotidienne", javascript)
        self.assertIn("Chaque lundi", javascript)
        self.assertIn("Visite réussie", javascript)
        self.assertIn("Nombre d’exécutions souhaité", javascript)
        self.assertIn("Tout sélectionner", javascript)
        self.assertIn("Tout effacer", javascript)
        self.assertIn("getallink.com/butter", javascript)
        self.assertIn("/api/state", javascript)
        self.assertIn("/api/targets", javascript)
        self.assertIn("/api/gateways", javascript)
        self.assertIn("/api/results/delete", javascript)
        self.assertIn("/api/settings", javascript)
        self.assertIn("/api/stop", javascript)
        self.assertIn("/results.csv", javascript)
        self.assertIn("Passerelle ou commande curl", javascript)
        self.assertIn("curl … -x …", javascript)
        self.assertIn("sorties limitées à US, UK et CA", javascript)
        self.assertIn("-country-GB", javascript)
        self.assertIn("Pays requis", javascript)
        self.assertIn("Rotation aléatoire", javascript)
        self.assertIn("En rotation", javascript)
        self.assertIn("Aucune URL enregistrée", javascript)
        self.assertIn("Aucune passerelle enregistrée", javascript)
        self.assertIn("color-scheme:dark", stylesheet.replace(" ", ""))
        self.assertIn("--accent:#a6c8da", stylesheet)
        self.assertIn("--bg:#0b0c0e", stylesheet)
        self.assertIn(".mobile-nav", stylesheet)
        self.assertIn(".country-pill", stylesheet)
        self.assertIn(".url-balance-grid", stylesheet)
        self.assertIn("@media (width<=720px)", stylesheet)
        self.assertNotIn("linear-gradient", stylesheet)
        self.assertNotIn("radial-gradient", stylesheet)
        self.assertNotIn("--canvas:#f4f4f1", stylesheet)
        self.assertIn(".empty-state", stylesheet)
        self.assertIn(".empty-icon", stylesheet)
        self.assertIn(".rotation-banner", stylesheet)

    def test_weekly_earnings_use_contiguous_average_daily_tiers(self):
        expected = {
            99.99: 0,
            100: 15,
            149.99: 15,
            150: 20,
            201.99: 20,
            202: 26,
            251.99: 26,
            252: 36,
            2_251.99: 206,
            2_252: 246,
            5_000: 246,
        }
        self.assertEqual(
            {
                clicks: web.weekly_payout_for_average(clicks)
                for clicks in expected
            },
            expected,
        )

        settings = {
            **web.DEFAULT_APP_SETTINGS,
            "targets": [
                {
                    "id": "target_weekly_123",
                    "alias": "Butter",
                    "url": "https://getallink.com/butter",
                    "button_text": "",
                    "second_button_text": "",
                    "selected": True,
                }
            ],
            "weekly_url_metrics": {
                "target_weekly_123": {
                    "2026-11-09": 120,
                    "2026-11-10": 150,
                    "2026-11-11": 180,
                }
            },
            "weekly_url_metrics_initialized": True,
        }
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

        earnings = web.weekly_earnings_for_api(settings, now=now)
        card = earnings["cards"][0]

        self.assertEqual(earnings["week_start"], "2026-11-09")
        self.assertEqual(earnings["week_end"], "2026-11-15")
        self.assertEqual(earnings["elapsed_days"], 3)
        self.assertEqual(card["week_clicks"], 450)
        self.assertEqual(card["average_daily_clicks"], 150)
        self.assertEqual(card["projected_payout"], 20)
        self.assertEqual(card["next_threshold"], 202)
        self.assertEqual(card["clicks_to_next"], 156)
        self.assertEqual(earnings["total_projected_payout"], 20)
        self.assertEqual(earnings["tiers"][0], {
            "minimum": 100,
            "maximum": 149,
            "payout": 15,
        })
        self.assertEqual(earnings["tiers"][1]["maximum"], 201)

        with mock.patch("web.cotonou_datetime", return_value=now):
            public = web.settings_for_api(settings)
        self.assertEqual(public["earnings_balance"], 20)
        self.assertEqual(
            public["weekly_earnings"]["cards"][0]["target_id"],
            "target_weekly_123",
        )

    def test_existing_persisted_reports_initialize_the_click_total(self):
        reports = [
            {
                "run_id": "existing_run_123",
                "results": [
                    {"success": True},
                    {"success": False},
                    {"success": True},
                ],
            }
        ]

        initialized = web.initialize_lifetime_metrics(
            web.load_app_settings(), reports
        )

        self.assertTrue(initialized["lifetime_metrics_initialized"])
        self.assertEqual(initialized["lifetime_successful_visits"], 2)
        self.assertEqual(
            web.load_app_settings()["lifetime_successful_visits"], 2
        )
        self.assertTrue(initialized["weekly_url_metrics_initialized"])

    def test_successful_visits_are_recorded_per_url_in_cotonou_time(self):
        web.save_app_settings(
            {
                **web.DEFAULT_APP_SETTINGS,
                "targets": [
                    {
                        "id": "target_weekly_123",
                        "alias": "Butter",
                        "url": "https://getallink.com/butter",
                        "button_text": "Continuer",
                        "second_button_text": "",
                        "selected": True,
                    }
                ],
                "lifetime_metrics_initialized": True,
                "redirect_success_migrated": True,
                "weekly_url_metrics_initialized": True,
            }
        )
        result = {
            "target_id": "target_weekly_123",
            "started_at": "2026-11-10T23:30:00+00:00",
            "initial_url": "https://getallink.com/butter",
            "final_url": "https://destination.example/page",
            "button_text": "Continuer",
            "success": True,
        }

        failed_result = {**result, "success": False}
        recorded = web.record_successful_visit_results(
            [result, failed_result]
        )
        settings = web.load_app_settings()

        self.assertEqual(recorded, 1)
        self.assertEqual(settings["lifetime_successful_visits"], 1)
        self.assertEqual(
            settings["weekly_url_metrics"]["target_weekly_123"][
                "2026-11-11"
            ],
            1,
        )

    def test_legacy_successes_without_a_redirect_are_corrected_once(self):
        web.save_app_settings(
            {
                **web.DEFAULT_APP_SETTINGS,
                "lifetime_successful_visits": 3,
                "lifetime_metrics_initialized": True,
                "redirect_success_migrated": False,
            }
        )
        reports = [
            {
                "run_id": "legacy_run_123",
                "results": [
                    {
                        "initial_url": "https://example.com/start",
                        "final_url": "https://example.com/start",
                        "button_text": "",
                        "success": True,
                    }
                    for _index in range(3)
                ],
            }
        ]

        migrated = web.initialize_lifetime_metrics(
            web.load_app_settings(), reports
        )
        public_reports = web.reports_for_api(reports)

        self.assertTrue(migrated["redirect_success_migrated"])
        self.assertEqual(migrated["lifetime_successful_visits"], 0)
        self.assertTrue(
            all(
                not result["success"]
                for result in public_reports[0]["results"]
            )
        )
        self.assertIn(
            "Aucune redirection",
            public_reports[0]["results"][0]["error"],
        )
        self.assertEqual(
            web.initialize_lifetime_metrics(migrated, reports)[
                "lifetime_successful_visits"
            ],
            0,
        )

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
        line = "sample-user:sample-pass-country-US@proxy.example:8000"
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
            self.gateway_path.read_text(encoding="utf-8"),
            f"# alias: passerelle-rotative\n{line}\n",
        )
        self.assertEqual(stat.S_IMODE(self.gateway_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.gateway_registry_path.stat().st_mode), 0o600
        )
        settings = web.load_app_settings()
        self.assertEqual(settings["gateways"][0]["alias"], "passerelle-rotative")
        self.assertNotIn("active", settings["gateways"][0])
        self.assertTrue(web.gateways_for_api(settings)[0]["eligible"])
        home = self.client.get("/", headers=self.headers)
        self.assertNotIn(line, home.get_data(as_text=True))

    def test_invalid_gateway_upload_does_not_replace_existing_secret_file(self):
        original = "sample-user:sample-pass-country-CA@proxy.example:8000\n"
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

    def test_gateway_registration_rejects_missing_or_unsupported_countries(self):
        values = (
            "sample-user:SENSITIVE_PASSWORD@proxy.example:8000",
            "sample-user:SENSITIVE_PASSWORD-country-FR@proxy.example:8000",
            "sample-user:SENSITIVE_PASSWORD-country-UK@proxy.example:8000",
        )

        for value in values:
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/gateways",
                    json={"alias": "Pays refusé", "gateway": value},
                    headers=self.api_headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("SENSITIVE_PASSWORD", response.get_data(as_text=True))

        self.assertEqual(web.load_app_settings()["gateways"], [])
        self.assertEqual(web.read_gateway_secrets(), {})
        self.assertFalse(self.gateway_path.exists())

    def test_legacy_gateway_without_allowed_country_is_never_rotated(self):
        legacy_secret = "sample-user:SENSITIVE_PASSWORD@proxy.example:8000"
        gateway_id = "gateway_legacy_countryless"
        web.save_app_settings(
            {
                **web.DEFAULT_APP_SETTINGS,
                "gateways": [{"id": gateway_id, "alias": "Ancienne"}],
            }
        )
        web.write_gateway_secrets({gateway_id: legacy_secret})

        state = self.client.get("/api/state", headers=self.headers)

        self.assertEqual(state.status_code, 200)
        self.assertFalse(state.get_json()["gateway_ready"])
        gateway = state.get_json()["gateways"][0]
        self.assertTrue(gateway["configured"])
        self.assertFalse(gateway["eligible"])
        self.assertEqual(gateway["country_code"], "")
        self.assertEqual(gateway["country"], "")
        self.assertNotIn("SENSITIVE_PASSWORD", state.get_data(as_text=True))
        self.assertFalse(self.gateway_path.exists())

    def test_gateway_crud_keeps_secrets_out_of_settings_and_handles_cache_loss(self):
        first_gateway = (
            "sample-user-a:sample-pass-a-country-US@proxy-a.example:8000"
        )
        second_gateway = (
            "sample-user-b:sample-pass-b-country-GB@proxy-b.example:8001"
        )
        replacement_gateway = (
            "sample-user-a:sample-pass-a-country-CA@proxy-a.example:8000"
        )

        created = self.client.post(
            "/api/gateways",
            json={
                "alias": "Rayobyte principale",
                "gateway": first_gateway,
            },
            headers=self.api_headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertNotIn(first_gateway, created.get_data(as_text=True))
        first = created.get_json()["gateways"][0]
        self.assertTrue(first["eligible"])
        self.assertTrue(first["configured"])
        self.assertEqual(first["country_code"], "US")
        self.assertEqual(first["country"], "US")
        first_id = first["id"]

        refused_update = self.client.patch(
            f"/api/gateways/{first_id}",
            json={
                "gateway": (
                    "sample-user-a:SENSITIVE_PASSWORD-country-FR"
                    "@proxy-a.example:8000"
                )
            },
            headers=self.api_headers,
        )
        self.assertEqual(refused_update.status_code, 400)
        self.assertNotIn(
            "SENSITIVE_PASSWORD", refused_update.get_data(as_text=True)
        )
        self.assertEqual(web.read_gateway_secrets()[first_id], first_gateway)

        raw_settings = self.settings_path.read_text(encoding="utf-8")
        self.assertIn("Rayobyte principale", raw_settings)
        self.assertNotIn("sample-pass-a", raw_settings)
        self.assertNotIn("proxy-a.example", raw_settings)
        self.assertEqual(
            stat.S_IMODE(self.gateway_registry_path.stat().st_mode), 0o600
        )

        created = self.client.post(
            "/api/gateways",
            json={
                "alias": "Rayobyte secours",
                "gateway": second_gateway,
            },
            headers=self.api_headers,
        )
        gateways = created.get_json()["gateways"]
        first_id = gateways[0]["id"]
        second_id = gateways[1]["id"]
        # Les deux passerelles restent éligibles : rotation aléatoire.
        self.assertTrue(gateways[0]["eligible"])
        self.assertTrue(gateways[1]["eligible"])
        self.assertEqual(gateways[1]["country_code"], "GB")
        self.assertEqual(gateways[1]["country"], "UK")
        pool_text = self.gateway_path.read_text(encoding="utf-8")
        self.assertIn("# alias: Rayobyte principale", pool_text)
        self.assertIn("# alias: Rayobyte secours", pool_text)
        self.assertIn(first_gateway, pool_text)
        self.assertIn(second_gateway, pool_text)
        self.assertEqual(stat.S_IMODE(self.gateway_path.stat().st_mode), 0o600)
        self.assertEqual(
            [gateway.name for gateway in main.load_proxy_gateways(
                self.gateway_path
            )],
            ["Rayobyte principale", "Rayobyte secours"],
        )
        state = self.client.get("/api/state", headers=self.headers)
        self.assertTrue(state.get_json()["gateway_ready"])

        updated = self.client.patch(
            f"/api/gateways/{first_id}",
            json={"alias": "Rayobyte prioritaire", "gateway": ""},
            headers=self.api_headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["gateways"][0]["configured"])
        self.assertEqual(updated.get_json()["gateways"][0]["country"], "US")

        updated = self.client.patch(
            f"/api/gateways/{first_id}",
            json={"gateway": replacement_gateway},
            headers=self.api_headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertNotIn(replacement_gateway, updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()["gateways"][0]["country_code"], "CA")
        self.assertEqual(updated.get_json()["gateways"][0]["country"], "CA")
        self.assertIn(
            replacement_gateway, self.gateway_path.read_text(encoding="utf-8")
        )

        deleted = self.client.delete(
            f"/api/gateways/{first_id}", headers=self.api_headers
        )
        self.assertEqual(deleted.status_code, 200)
        remaining = deleted.get_json()["gateways"]
        self.assertEqual([item["id"] for item in remaining], [second_id])
        self.assertTrue(remaining[0]["eligible"])
        self.assertNotIn(
            replacement_gateway, self.gateway_path.read_text(encoding="utf-8")
        )

        self.gateway_registry_path.unlink()
        with web.GATEWAY_SECRETS_LOCK:
            web.GATEWAY_SECRETS_CACHE = None
        state = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(state.status_code, 200)
        persisted = state.get_json()["gateways"]
        self.assertEqual(persisted[0]["alias"], "Rayobyte secours")
        self.assertFalse(persisted[0]["eligible"])
        self.assertFalse(persisted[0]["configured"])
        self.assertEqual(persisted[0]["country_code"], "")
        self.assertEqual(persisted[0]["country"], "")
        self.assertFalse(state.get_json()["gateway_ready"])
        self.assertFalse(self.gateway_path.exists())

        deleted = self.client.delete(
            f"/api/gateways/{second_id}", headers=self.api_headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["gateways"], [])
        self.assertEqual(web.load_app_settings()["gateways"], [])
        self.assertFalse(self.gateway_path.exists())

    def test_rotation_pool_exposes_every_valid_gateway_to_the_engine(self):
        values = (
            ("Rayobyte US", "user-a:pass-a-country-US@proxy-a.example:8000"),
            ("Rayobyte UK", "user-b:pass-b-country-GB@proxy-b.example:8001"),
            ("Rayobyte CA", "user-c:pass-c-country-CA@proxy-c.example:8002"),
        )
        for alias, value in values:
            response = self.client.post(
                "/api/gateways",
                json={"alias": alias, "gateway": value},
                headers=self.api_headers,
            )
            self.assertEqual(response.status_code, 200)

        state = self.client.get("/api/state", headers=self.headers)
        payload = state.get_json()

        self.assertTrue(payload["gateway_ready"])
        self.assertEqual(
            [item["eligible"] for item in payload["gateways"]], [True, True, True]
        )
        self.assertEqual(
            [item["country"] for item in payload["gateways"]], ["US", "UK", "CA"]
        )
        for _, value in values:
            self.assertNotIn(value, state.get_data(as_text=True))

        pool = main.load_proxy_gateways(self.gateway_path)
        self.assertEqual(
            [gateway.name for gateway in pool],
            ["Rayobyte US", "Rayobyte UK", "Rayobyte CA"],
        )
        self.assertEqual(
            sorted(gateway.country_code for gateway in pool), ["CA", "GB", "US"]
        )
        self.assertEqual(stat.S_IMODE(self.gateway_path.stat().st_mode), 0o600)

    def test_rotation_rejects_the_removed_single_gateway_selection_route(self):
        gateway_id = "gateway_selection_removed"
        web.save_app_settings(
            {
                **web.DEFAULT_APP_SETTINGS,
                "gateways": [{"id": gateway_id, "alias": "Unique"}],
            }
        )
        web.write_gateway_secrets(
            {gateway_id: "sample-user:sample-pass-country-US@proxy.example:8000"}
        )

        response = self.client.post(
            f"/api/gateways/{gateway_id}/select",
            json={},
            headers=self.api_headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_gateway_credentials_survive_redeployment_with_supabase_vault(self):
        gateway_values = {
            "gateway_persistent": (
                "sample-user:sample-pass-country-CA@persistent.proxy.example:8000"
            )
        }
        storage = mock.Mock()
        storage.load_gateway_secrets.return_value = gateway_values

        with mock.patch.object(
            web.SupabaseStorage, "from_env", return_value=storage
        ):
            web.write_gateway_secrets(gateway_values)
            self.gateway_registry_path.unlink()
            with web.GATEWAY_SECRETS_LOCK:
                web.GATEWAY_SECRETS_CACHE = None
            restored = web.read_gateway_secrets()

        self.assertEqual(restored, gateway_values)
        storage.save_gateway_secrets.assert_called_once_with(gateway_values)
        storage.load_gateway_secrets.assert_called_once_with()
        self.assertEqual(
            stat.S_IMODE(self.gateway_registry_path.stat().st_mode), 0o600
        )

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
            [
                (
                    item["alias"],
                    item["url"],
                    item["button_text"],
                    item["second_button_text"],
                    item["selected"],
                )
                for item in settings["targets"]
            ],
            [
                (
                    "first.example",
                    "https://first.example/page",
                    "Continuer",
                    "Confirmer",
                    True,
                ),
                (
                    "unrelated.example.org",
                    "https://unrelated.example.org/other",
                    "Valider",
                    "Terminer",
                    True,
                ),
            ],
        )
        self.assertTrue(
            all(item["id"].startswith("target_") for item in settings["targets"])
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
        target = web.load_app_settings()["targets"][0]
        self.assertEqual(target["alias"], "first.example")
        self.assertEqual(target["url"], "https://first.example/page")
        self.assertEqual(target["button_text"], "Continuer")
        self.assertEqual(target["second_button_text"], "")
        self.assertTrue(target["selected"])
        self.assertEqual(
            web.targets_to_text(web.load_app_settings()),
            "https://first.example/page | Continuer",
        )

    def test_target_upload_accepts_a_url_without_button_labels(self):
        response = self.post_file(
            "/upload-targets",
            "target_file",
            "first.example/page\n",
            "targets.txt",
        )

        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(query["targets"], ["1"])
        target = web.load_app_settings()["targets"][0]
        self.assertEqual(target["button_text"], "")
        self.assertEqual(target["second_button_text"], "")
        self.assertEqual(
            web.targets_to_text(web.load_app_settings()),
            "https://first.example/page",
        )

    def test_target_upload_rejects_a_second_label_without_the_first(self):
        response = self.post_file(
            "/upload-targets",
            "target_file",
            "https://first.example/page | | Confirmer\n",
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
        self.assertEqual(len(settings["targets"]), 1)
        self.assertEqual(settings["targets"][0]["alias"], "edited.example")
        self.assertEqual(
            settings["targets"][0]["url"], "https://edited.example/page"
        )
        self.assertTrue(settings["targets"][0]["selected"])

    def test_target_crud_and_persistent_selection_use_json_without_redirects(self):
        created = self.client.post(
            "/api/targets",
            json={
                "alias": "Accueil partenaire",
                "url": "https://partner.example/page",
                "button_text": "Continuer",
                "second_button_text": "Confirmer",
                "selected": True,
            },
            headers=self.api_headers,
        )
        self.assertEqual(created.status_code, 200)
        created_value = created.get_json()
        self.assertTrue(created_value["ok"])
        self.assertEqual(len(created_value["targets"]), 1)
        target_id = created_value["targets"][0]["id"]

        listed = self.client.get("/api/targets", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            listed.get_json()["targets"][0]["alias"], "Accueil partenaire"
        )

        updated = self.client.patch(
            f"/api/targets/{target_id}",
            json={"alias": "Partenaire principal", "selected": False},
            headers=self.api_headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["targets"][0]["alias"], "Partenaire principal")
        self.assertFalse(updated.get_json()["targets"][0]["selected"])
        self.assertEqual(web.targets_from_settings(web.load_app_settings()), [])

        selected = self.client.post(
            "/api/targets/selection",
            json={"mode": "all"},
            headers=self.api_headers,
        )
        self.assertEqual(selected.status_code, 200)
        active_targets = web.targets_from_settings(web.load_app_settings())
        self.assertEqual(len(active_targets), 1)
        self.assertEqual(active_targets[0].alias, "Partenaire principal")
        self.assertEqual(active_targets[0].target_id, target_id)

        deleted = self.client.delete(
            f"/api/targets/{target_id}", headers=self.api_headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["targets"], [])

    def test_target_crud_accepts_no_labels_and_rejects_only_a_second_label(self):
        created = self.client.post(
            "/api/targets",
            json={
                "alias": "Visite simple",
                "url": "https://visit.example/page",
                "button_text": "",
                "second_button_text": "",
                "selected": True,
            },
            headers=self.api_headers,
        )
        self.assertEqual(created.status_code, 200)
        target = created.get_json()["targets"][0]
        self.assertEqual(target["button_text"], "")
        self.assertEqual(target["second_button_text"], "")
        selected = web.targets_from_settings(web.load_app_settings())
        self.assertEqual(selected[0].button_text, "")

        rejected = self.client.post(
            "/api/targets",
            json={
                "alias": "Configuration invalide",
                "url": "https://invalid.example/page",
                "button_text": "",
                "second_button_text": "Confirmer",
                "selected": True,
            },
            headers=self.api_headers,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("premier libellé", rejected.get_json()["error"])
        self.assertEqual(len(web.load_app_settings()["targets"]), 1)

    def test_api_mutations_require_csrf_and_reject_changes_while_running(self):
        missing_csrf = self.client.post(
            "/api/targets",
            json={
                "alias": "Test",
                "url": "https://example.com",
                "button_text": "Entrer",
            },
            headers=self.headers,
        )
        self.assertEqual(missing_csrf.status_code, 400)

        with web.STATE_LOCK:
            web.RUN_STATE["running"] = True
        blocked = self.client.post(
            "/api/targets",
            json={
                "alias": "Test",
                "url": "https://example.com",
                "button_text": "Entrer",
            },
            headers=self.api_headers,
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("arrêt", blocked.get_json()["error"])

    def test_settings_support_an_interval_campaign_and_continuous_mode(self):
        target = "https://edited.example/page | Continuer"
        response = self.client.post(
            "/settings",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "targets": target,
                "interval_minutes": "60",
                "execution_mode": "range",
                "repetition_count": "7",
                "repetition_min": "3",
                "repetition_max": "9",
                "interval_execution_count": "80",
                "interval_duration_days": "7",
                "interval_repetition_count": "4",
            },
            headers=self.headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        settings = web.load_app_settings()
        self.assertEqual(settings["execution_mode"], "range")
        self.assertEqual(settings["repetition_count"], 7)
        self.assertEqual(settings["repetition_min"], 3)
        self.assertEqual(settings["repetition_max"], 9)
        self.assertEqual(settings["interval_execution_count"], 80)
        self.assertEqual(settings["interval_duration_days"], 7)
        self.assertEqual(settings["interval_repetition_count"], 4)
        self.assertFalse(settings["schedule_enabled"])

        response = self.client.post(
            "/settings",
            data={
                "csrf_token": web.CSRF_TOKEN,
                "targets": target,
                "interval_minutes": "60",
                "execution_mode": "continuous",
                "repetition_count": "7",
                "repetition_min": "3",
                "repetition_max": "9",
            },
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            web.load_app_settings()["execution_mode"], "continuous"
        )

    def test_run_api_starts_an_interval_campaign_instead_of_one_long_job(self):
        settings = {**web.DEFAULT_APP_SETTINGS, "execution_mode": "range"}
        campaign = {"total_executions": 23}
        with (
            mock.patch("web.load_app_settings", return_value=settings),
            mock.patch("web.sync_gateway_pool", return_value=True),
            mock.patch("web.load_browser_settings", return_value=object()),
            mock.patch(
                "web.targets_from_settings",
                return_value=[main.Target("https://example.com")],
            ),
            mock.patch(
                "web.start_interval_campaign", return_value=campaign
            ) as start_campaign,
            mock.patch("web.start_job") as start_job,
        ):
            response = self.client.post(
                "/api/run", json={}, headers=self.api_headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("23 exécution", response.get_json()["message"])
        start_campaign.assert_called_once_with()
        start_job.assert_not_called()

    def test_interval_campaign_uses_plus_or_minus_ten_random_times(self):
        offsets = list(reversed(range(30)))
        with (
            mock.patch("web.random.randint", return_value=30) as choose_total,
            mock.patch("web.random.uniform", side_effect=offsets),
        ):
            campaign = web.build_interval_campaign(
                20, 3, now_timestamp=1_000
            )

        choose_total.assert_called_once_with(10, 30)
        self.assertEqual(campaign["total_executions"], 30)
        self.assertEqual(campaign["launched_executions"], 0)
        self.assertEqual(
            campaign["scheduled_timestamps"],
            [1_000 + offset for offset in sorted(offsets)],
        )
        self.assertTrue(campaign["active"])

    def test_interval_campaign_launches_independent_single_pass_reports(self):
        campaign = {
            "id": "campaign_test_123",
            "active": True,
            "started_at": "2026-11-04T00:00:00+00:00",
            "ends_at": "2026-11-05T00:00:00+00:00",
            "total_executions": 2,
            "launched_executions": 0,
            "scheduled_timestamps": [900.0, 1_100.0],
        }
        settings = {
            **web.DEFAULT_APP_SETTINGS,
            "execution_mode": "range",
            "interval_repetition_count": 4,
            "interval_campaign": campaign,
        }
        with (
            mock.patch("web.load_app_settings", return_value=settings),
            mock.patch("web.save_app_settings") as save_settings,
            mock.patch("web.start_job", return_value=True) as start_job,
            mock.patch("web.record_database_event"),
        ):
            started = web.process_interval_campaign(now_timestamp=1_000)

        self.assertTrue(started)
        start_job.assert_called_once_with(
            "intervalle 1/2",
            execution_mode_override="range",
            repetition_count_override=4,
        )
        saved_campaign = save_settings.call_args.args[0]["interval_campaign"]
        self.assertEqual(saved_campaign["launched_executions"], 1)
        self.assertEqual(saved_campaign["scheduled_timestamps"], [1_100.0])
        self.assertTrue(saved_campaign["active"])

    def test_stopping_between_interval_executions_cancels_the_campaign(self):
        settings = {
            **web.DEFAULT_APP_SETTINGS,
            "interval_campaign": {
                "id": "campaign_stop_123",
                "active": True,
                "started_at": "now",
                "ends_at": "later",
                "total_executions": 12,
                "launched_executions": 2,
                "scheduled_timestamps": [time.time() + 3_600],
            },
        }
        web.save_app_settings(settings)

        self.assertTrue(web.request_job_stop())

        campaign = web.load_app_settings()["interval_campaign"]
        self.assertFalse(campaign["active"])
        self.assertEqual(campaign["scheduled_timestamps"], [])
        self.assertFalse(web.RUN_STATE["interval_campaign_active"])

    def test_local_report_retention_keeps_only_the_latest_500_visits(self):
        reports = [
            {
                "run_id": "run_old_123",
                "completed_visits": 300,
                "results": [{"value": value} for value in range(300)],
            },
            {
                "run_id": "run_new_123",
                "completed_visits": 300,
                "results": [{"value": value} for value in range(300, 600)],
            },
        ]

        retained = web.retain_latest_visits(reports)

        self.assertEqual(sum(len(item["results"]) for item in retained), 500)
        self.assertEqual(retained[0]["results"][0]["value"], 100)
        self.assertEqual(retained[-1]["results"][-1]["value"], 599)

    def test_supabase_is_the_primary_settings_store_with_a_local_cache(self):
        persistent_settings = {
            "targets": [
                {
                    "id": "target_persistent",
                    "alias": "Persistante",
                    "url": "https://persistent.example/page",
                    "button_text": "Continuer",
                    "second_button_text": "",
                    "selected": True,
                }
            ],
            "gateways": [],
            "schedule_enabled": True,
            "interval_minutes": 180,
            "execution_mode": "range",
            "repetition_count": 4,
            "repetition_min": 2,
            "repetition_max": 6,
            "interval_execution_count": 20,
            "interval_duration_days": 1,
            "interval_repetition_count": 1,
            "interval_campaign": {},
            "lifetime_successful_visits": 0,
            "lifetime_metrics_initialized": False,
            "redirect_success_migrated": False,
            "weekly_url_metrics": {},
            "weekly_url_metrics_initialized": False,
        }
        storage = mock.Mock()
        storage.load_settings.return_value = persistent_settings

        with mock.patch.object(
            web.SupabaseStorage, "from_env", return_value=storage
        ):
            settings = web.load_app_settings()

        self.assertEqual(settings, persistent_settings)
        self.assertEqual(
            web.read_local_app_settings(),
            persistent_settings,
        )
        with web.DATABASE_LOCK:
            self.assertTrue(web.DATABASE_STATE["connected"])

    def test_partial_supabase_configuration_refuses_a_divergent_write(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SUPABASE_URL": "https://project.supabase.co",
                    "SUPABASE_SECRET_KEY": "",
                },
            ),
            self.assertRaises(main.ConfigurationError),
        ):
            web.save_app_settings(dict(web.DEFAULT_APP_SETTINGS))

        self.assertFalse(self.settings_path.exists())

    def test_progress_is_cached_and_sent_to_supabase(self):
        storage = mock.Mock()
        report = {
            "run_id": "persistent_run",
            "completed_visits": 1,
            "results": [{"started_at": "now", "success": True}],
        }

        with mock.patch.object(
            web.SupabaseStorage, "from_env", return_value=storage
        ):
            web.upsert_report(report)

        storage.upsert_report.assert_called_once_with(report)
        self.assertEqual(web.read_reports_unlocked(), [report])

    def test_stop_route_requests_a_clean_job_stop(self):
        with web.STATE_LOCK:
            web.RUN_STATE["running"] = True

        response = self.client.post(
            "/stop",
            data={"csrf_token": web.CSRF_TOKEN},
            headers=self.headers,
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(web.STOP_EVENT.is_set())
        with web.STATE_LOCK:
            self.assertTrue(web.RUN_STATE["stop_requested"])

    def test_job_upserts_results_after_each_visit(self):
        web.save_app_settings(
            {
                "targets": [
                    {
                        "url": "https://example.com/page",
                        "button_text": "Entrer",
                        "second_button_text": "",
                    }
                ],
                "schedule_enabled": False,
                "interval_minutes": 60,
                "execution_mode": "fixed",
                "repetition_count": 2,
                "repetition_min": 1,
                "repetition_max": 10,
            }
        )

        def fake_run_targets(_settings, targets, **kwargs):
            report = web.RunReport(
                started_at="now",
                execution_mode="fixed",
                repetition_count=2,
            )
            for repetition in (1, 2):
                report.results.append(
                    main.VisitResult(
                        started_at=f"visit-{repetition}",
                        initial_url=targets[0].url,
                        button_text=targets[0].button_text,
                        second_button_text="",
                        target_id=targets[0].target_id,
                        repetition=repetition,
                        success=repetition == 1,
                    )
                )
                report.completed_repetitions = repetition
                report.completed_visits = repetition
                kwargs["on_progress"](report)
            report.success = True
            report.finished_at = "done"
            return report

        with (
            mock.patch("web.load_browser_settings", return_value=object()),
            mock.patch("web.run_targets", side_effect=fake_run_targets),
            mock.patch("web.upsert_report", wraps=web.upsert_report) as upsert,
        ):
            web.execute_job("manuel")

        self.assertEqual(upsert.call_count, 3)
        snapshots = [call.args[0] for call in upsert.call_args_list]
        self.assertEqual(
            [len(snapshot["results"]) for snapshot in snapshots], [1, 2, 2]
        )
        self.assertEqual(
            [snapshot["in_progress"] for snapshot in snapshots],
            [True, True, False],
        )
        reports = web.load_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]["results"]), 2)
        self.assertFalse(reports[0]["in_progress"])
        saved_settings = web.load_app_settings()
        self.assertEqual(saved_settings["lifetime_successful_visits"], 1)
        target_id = saved_settings["targets"][0]["id"]
        self.assertEqual(
            sum(saved_settings["weekly_url_metrics"][target_id].values()),
            1,
        )

    def test_results_can_be_deleted_by_visit_run_or_all_from_the_api(self):
        web.append_report(
            {
                "run_id": "run_selective_123",
                "source": "manuel",
                "completed_visits": 2,
                "results": [
                    {"started_at": "first", "success": True},
                    {"started_at": "second", "success": False},
                ],
            }
        )

        with web.STATE_LOCK:
            web.RUN_STATE["running"] = True
        visit_deleted = self.client.post(
            "/api/results/delete",
            json={
                "action": "selected",
                "visits": [
                    {"run_id": "run_selective_123", "visit_number": 1}
                ],
                "run_ids": [],
            },
            headers=self.api_headers,
        )
        self.assertEqual(visit_deleted.status_code, 200)
        self.assertEqual(
            visit_deleted.get_json()["summary"], {"visits": 1, "runs": 0}
        )
        self.assertIn("exécution en cours", visit_deleted.get_json()["message"])
        remaining = visit_deleted.get_json()["reports"][0]["results"]
        self.assertEqual([item["started_at"] for item in remaining], ["second"])

        run_deleted = self.client.delete(
            "/api/results",
            json={
                "action": "selected",
                "visits": [],
                "run_ids": ["run_selective_123"],
            },
            headers=self.api_headers,
        )
        self.assertEqual(run_deleted.status_code, 200)
        self.assertEqual(run_deleted.get_json()["reports"], [])

        web.append_report(
            {
                "run_id": "run_without_visits_123",
                "completed_visits": 0,
                "results": [],
            }
        )
        empty_run_deleted = self.client.post(
            "/api/results/delete",
            json={
                "action": "selected",
                "visits": [],
                "run_ids": ["run_without_visits_123"],
            },
            headers=self.api_headers,
        )
        self.assertEqual(empty_run_deleted.status_code, 200)
        self.assertEqual(empty_run_deleted.get_json()["reports"], [])

        web.append_report(
            {
                "run_id": "run_clear_123",
                "completed_visits": 1,
                "results": [{"started_at": "third", "success": True}],
            }
        )
        cleared = self.client.post(
            "/api/results/delete",
            json={"action": "all"},
            headers=self.api_headers,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["reports"], [])
        self.assertIn("Historique Supabase effacé", cleared.get_json()["message"])

    def test_result_deletion_calls_supabase_and_refreshes_the_interface(self):
        storage = mock.Mock()
        storage.delete_results.return_value = {"visits": 2, "runs": 1}
        storage.load_reports.return_value = []

        with mock.patch.object(
            web.SupabaseStorage, "from_env", return_value=storage
        ):
            response = self.client.post(
                "/api/results/delete",
                json={
                    "action": "selected",
                    "visits": [
                        {"run_id": "run_supabase_123", "visit_number": 2}
                    ],
                    "run_ids": ["run_supabase_456"],
                },
                headers=self.api_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], {"visits": 2, "runs": 1})
        self.assertEqual(response.get_json()["reports"], [])
        storage.delete_results.assert_called_once_with(
            visits=[{"run_id": "run_supabase_123", "visit_number": 2}],
            run_ids=["run_supabase_456"],
        )
        storage.load_reports.assert_called_once_with(all_data=False)

    def test_result_deletion_reports_safe_supabase_configuration_errors(self):
        with mock.patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "invalid-test-value",
            },
        ):
            response = self.client.post(
                "/api/results/delete",
                json={"action": "all"},
                headers=self.api_headers,
            )

        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertIn("SUPABASE_SECRET_KEY", error)
        self.assertNotIn("invalid-test-value", error)

    def test_result_deletion_rejects_unknown_actions(self):
        response = self.client.post(
            "/api/results/delete",
            json={"action": "unexpected"},
            headers=self.api_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("inconnue", response.get_json()["error"])

    def test_csv_includes_the_execution_mode_without_ip_check_columns(self):
        web.append_report(
            {
                "source": "manuel",
                "execution_mode": "range",
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
                    },
                    {
                        "started_at": "2026-10-30T00:01:00+00:00",
                        "repetition": 8,
                        "proxy_name": "passerelle-rotative",
                        "proxy_attempts": 1,
                        "initial_url": "https://visit-only.example/page",
                        "button_text": "",
                        "second_button_text": "",
                        "final_url": "https://visit-only.example/page",
                        "success": True,
                    },
                ],
            }
        )

        response = self.client.get("/results.csv", headers=self.headers)
        content = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("cible_id,pseudo_cible,mode_execution", content)
        self.assertIn("mode_execution,repetition", content)
        self.assertIn("repetition,repetitions_planifiees", content)
        self.assertIn(
            ",range,7,200,,,passerelle-rotative,1,", content
        )
        rows = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(rows[1]["premier_libelle"], "")
        self.assertEqual(rows[1]["second_libelle"], "")
        self.assertEqual(
            rows[1]["url_finale"], "https://visit-only.example/page"
        )
        self.assertNotIn("ip_sortie", content)
        self.assertNotIn("pays", content)

    def test_csv_automatically_keeps_only_the_latest_500_rows(self):
        web.append_report(
            {
                "run_id": "run_csv_retention_123",
                "source": "manuel",
                "execution_mode": "fixed",
                "repetition_count": 600,
                "completed_visits": 600,
                "results": [
                    {
                        "started_at": f"visit-{index}",
                        "repetition": index,
                        "initial_url": f"https://example.com/{index}",
                        "final_url": f"https://example.com/{index}",
                        "success": True,
                    }
                    for index in range(1, 601)
                ],
            }
        )

        response = self.client.get("/results.csv", headers=self.headers)
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))

        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["url_initiale"], "https://example.com/101")
        self.assertEqual(rows[-1]["url_initiale"], "https://example.com/600")

    def test_upload_routes_require_authentication_and_csrf(self):
        unauthorized = self.client.post("/gateway")
        self.assertEqual(unauthorized.status_code, 401)

        missing_csrf = self.client.post(
            "/gateway",
            data={
                "gateway": "sample-user:sample-pass-country-US@proxy.example:8000"
            },
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
