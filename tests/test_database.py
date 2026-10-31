import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError

from database import StorageError, SupabaseStorage


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self):
        return self.payload


class SupabaseStorageTests(unittest.TestCase):
    def test_environment_requires_url_and_secret_together(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(SupabaseStorage.from_env())

        with (
            mock.patch.dict(
                os.environ,
                {"SUPABASE_URL": "https://project.supabase.co"},
                clear=True,
            ),
            self.assertRaises(StorageError),
        ):
            SupabaseStorage.from_env()

    def test_repr_and_errors_never_echo_the_secret(self):
        secret = "sb_secret_value_that_must_stay_private_12345"
        storage = SupabaseStorage("https://project.supabase.co", secret)
        self.assertNotIn(secret, repr(storage))

        error = HTTPError(
            "https://project.supabase.co/rest/v1/table",
            403,
            "forbidden",
            {},
            io.BytesIO(f"sensitive: {secret}".encode()),
        )
        with (
            mock.patch("database.urlopen", side_effect=error),
            self.assertRaises(StorageError) as caught,
        ):
            storage.check()
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("HTTP 403", str(caught.exception))

    def test_save_settings_uses_server_headers_and_an_upsert(self):
        storage = SupabaseStorage(
            "https://project.supabase.co", "sb_secret_value_for_tests_123456789"
        )
        with mock.patch(
            "database.urlopen", return_value=FakeResponse()
        ) as urlopen:
            storage.save_settings({"execution_mode": "fixed"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("ghost_clicker_settings?on_conflict=id", request.full_url)
        self.assertEqual(
            request.get_header("Apikey"),
            "sb_secret_value_for_tests_123456789",
        )
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("resolution=merge-duplicates", request.headers["Prefer"])
        self.assertEqual(
            json.loads(request.data),
            {
                "id": "default",
                "payload": {"execution_mode": "fixed"},
            },
        )

    def test_progress_upserts_summary_and_latest_visit_atomically(self):
        storage = SupabaseStorage(
            "https://project.supabase.co", "sb_secret_value_for_tests_123456789"
        )
        report = {
            "run_id": "run_123",
            "completed_visits": 12,
            "success": False,
            "results": [
                {"started_at": "old"},
                {"started_at": "latest", "success": True},
            ],
        }
        with mock.patch.object(SupabaseStorage, "_request") as request:
            run_id = storage.upsert_report(report)

        self.assertEqual(run_id, "run_123")
        body = request.call_args.kwargs["body"]
        self.assertEqual(request.call_args.args[:2], ("POST", "rpc/ghost_clicker_upsert_report"))
        self.assertNotIn("results", body["p_report"])
        self.assertEqual(body["p_visit_number"], 12)
        self.assertEqual(body["p_visit"]["started_at"], "latest")

    def test_load_reports_rebuilds_ordered_results(self):
        storage = SupabaseStorage(
            "https://project.supabase.co", "sb_secret_value_for_tests_123456789"
        )

        def fake_request(_method, path, **_kwargs):
            if path == "ghost_clicker_reports":
                return [
                    {
                        "run_id": "new_run",
                        "payload": {"source": "manuel"},
                        "updated_at": "2026-10-31T12:00:00Z",
                    },
                    {
                        "run_id": "old_run",
                        "payload": {"source": "planifié"},
                        "updated_at": "2026-10-30T12:00:00Z",
                    },
                ]
            if path == "ghost_clicker_visits":
                return [
                    {
                        "run_id": "new_run",
                        "visit_number": 2,
                        "payload": {"repetition": 2},
                    },
                    {
                        "run_id": "new_run",
                        "visit_number": 1,
                        "payload": {"repetition": 1},
                    },
                ]
            self.fail(path)

        with mock.patch.object(
            SupabaseStorage, "_request", side_effect=fake_request
        ):
            reports = storage.load_reports()

        self.assertEqual([item["run_id"] for item in reports], ["old_run", "new_run"])
        self.assertEqual(
            [item["repetition"] for item in reports[1]["results"]],
            [1, 2],
        )
        self.assertEqual(reports[0]["results"], [])


if __name__ == "__main__":
    unittest.main()
