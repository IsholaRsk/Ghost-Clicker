import base64
import csv
import io
import os
import stat
import tempfile
import time
import unittest
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

    def test_home_uses_large_text_flat_design_and_button_only_navigation(self):
        response = self.client.get("/", headers=self.headers)
        content = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Vue d’ensemble", content)
        self.assertIn("Bibliothèque d’URL", content)
        self.assertIn("Passerelles Rayobyte", content)
        self.assertIn("Résultats Supabase", content)
        self.assertIn("color-scheme:dark", content)
        self.assertIn("--canvas:#0d0f12", content)
        self.assertIn('id="app-track" class="app-track"', content)
        self.assertIn('class="app-page active" data-page="home"', content)
        self.assertIn('class="app-page" data-page="targets"', content)
        self.assertIn('class="app-page" data-page="gateways"', content)
        self.assertIn('class="app-page" data-page="results"', content)
        self.assertIn('class="app-page" data-page="settings"', content)
        self.assertIn('class="bottom-nav"', content)
        self.assertIn('data-nav="gateways"', content)
        self.assertIn("node.classList.toggle('active',!hidden)", content)
        self.assertIn("font:16px/1.6", content)
        self.assertIn("height:48px", content)
        self.assertIn("Premier libellé facultatif", content)
        self.assertIn('id="target-button" placeholder="Continuer">', content)
        self.assertIn('id="target-editor" class="panel" hidden', content)
        self.assertIn('id="gateway-editor" class="panel" hidden', content)
        self.assertIn('id="mode-parameters" class="mode-parameters" hidden', content)
        self.assertIn("Nombre d’exécutions souhaité", content)
        self.assertIn("Durée en jours", content)
        self.assertIn("Répétitions par exécution", content)
        self.assertIn("moins 10 et plus 10", content)
        self.assertIn("revealModeSettings", content)
        self.assertIn("show-target-editor", content)
        self.assertIn("show-gateway-editor", content)
        self.assertIn("password-country-CA@us-east.gw.rayobyte.com:8000", content)
        self.assertIn("sans crochets, lien mailto ni Markdown", content)
        self.assertIn(".panel{margin-bottom:30px;border-top:1px solid", content)
        self.assertIn(".stat:last-child{border-right:0}", content)
        self.assertIn(".badge{display:inline-block;font-size:12px", content)
        self.assertNotIn("border-radius:999px", content)
        self.assertNotIn("style.transform", content)
        self.assertNotIn("touchstart", content)
        self.assertNotIn("touchend", content)
        self.assertNotIn("hashchange", content)
        self.assertIn("Base de données", content)
        self.assertIn("Cache local", content)
        self.assertIn("Google Chrome neuf par visite", content)
        self.assertIn('value="continuous"', content)
        self.assertIn("Cycles de cinq minutes", content)
        self.assertIn("Arrêter", content)
        self.assertIn("/api/targets", content)
        self.assertIn("/api/gateways", content)
        self.assertIn("/api/results/delete", content)
        self.assertIn("La suppression reste disponible pendant une exécution", content)
        self.assertIn("exécution(s) sans visite", content)
        self.assertIn("dataset.kind", content)
        self.assertIn("fetch(url,options)", content)
        self.assertNotIn('http-equiv="refresh"', content)
        self.assertNotIn("Importer les cibles", content)
        self.assertNotIn("129–283", content)
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
            self.gateway_path.read_text(encoding="utf-8"),
            f"# alias: passerelle-rotative\n{line}\n",
        )
        self.assertEqual(stat.S_IMODE(self.gateway_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.gateway_registry_path.stat().st_mode), 0o600
        )
        settings = web.load_app_settings()
        self.assertEqual(settings["gateways"][0]["alias"], "passerelle-rotative")
        self.assertTrue(settings["gateways"][0]["active"])
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

    def test_gateway_crud_keeps_secrets_out_of_settings_and_handles_cache_loss(self):
        first_gateway = "sample-user-a:sample-pass-a@proxy-a.example:8000"
        second_gateway = "sample-user-b:sample-pass-b@proxy-b.example:8001"

        created = self.client.post(
            "/api/gateways",
            json={
                "alias": "Rayobyte principale",
                "gateway": first_gateway,
                "active": False,
            },
            headers=self.api_headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertNotIn(first_gateway, created.get_data(as_text=True))
        first = created.get_json()["gateways"][0]
        self.assertTrue(first["active"])
        self.assertTrue(first["configured"])

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
                "active": True,
            },
            headers=self.api_headers,
        )
        gateways = created.get_json()["gateways"]
        first_id = gateways[0]["id"]
        second_id = gateways[1]["id"]
        self.assertFalse(gateways[0]["active"])
        self.assertTrue(gateways[1]["active"])
        active_text = self.gateway_path.read_text(encoding="utf-8")
        self.assertIn("# alias: Rayobyte secours", active_text)
        self.assertIn(second_gateway, active_text)

        updated = self.client.patch(
            f"/api/gateways/{first_id}",
            json={"alias": "Rayobyte prioritaire", "gateway": ""},
            headers=self.api_headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["gateways"][0]["configured"])

        selected = self.client.post(
            f"/api/gateways/{first_id}/select",
            json={},
            headers=self.api_headers,
        )
        self.assertEqual(selected.status_code, 200)
        self.assertTrue(selected.get_json()["gateways"][0]["active"])
        self.assertFalse(selected.get_json()["gateways"][1]["active"])

        deleted = self.client.delete(
            f"/api/gateways/{first_id}", headers=self.api_headers
        )
        self.assertEqual(deleted.status_code, 200)
        remaining = deleted.get_json()["gateways"]
        self.assertEqual([item["id"] for item in remaining], [second_id])
        self.assertTrue(remaining[0]["active"])

        self.gateway_registry_path.unlink()
        with web.GATEWAY_SECRETS_LOCK:
            web.GATEWAY_SECRETS_CACHE = None
        state = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(state.status_code, 200)
        persisted = state.get_json()["gateways"]
        self.assertEqual(persisted[0]["alias"], "Rayobyte secours")
        self.assertTrue(persisted[0]["active"])
        self.assertFalse(persisted[0]["configured"])
        self.assertFalse(self.gateway_path.exists())

        refused = self.client.post(
            f"/api/gateways/{second_id}/select",
            json={},
            headers=self.api_headers,
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Ressaisissez", refused.get_json()["error"])

        deleted = self.client.delete(
            f"/api/gateways/{second_id}", headers=self.api_headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["gateways"], [])
        self.assertEqual(web.load_app_settings()["gateways"], [])
        self.assertFalse(self.gateway_path.exists())

    def test_gateway_credentials_survive_redeployment_with_supabase_vault(self):
        gateway_values = {
            "gateway_persistent": (
                "sample-user:sample-pass@persistent.proxy.example:8000"
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
            "https://first.example/page\n",
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
            mock.patch("web.sync_active_gateway", return_value=True),
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
                        repetition=repetition,
                        success=True,
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
