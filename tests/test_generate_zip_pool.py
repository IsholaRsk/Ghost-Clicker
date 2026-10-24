import random
import tempfile
import unittest
from pathlib import Path

import generate_zip_pool


class ZipPoolTests(unittest.TestCase):
    def test_country_password_prefix_removes_old_targeting(self):
        value = "secret-country-US-zip-75032-asn-7018-session-a1b2c3d4"
        self.assertEqual(
            generate_zip_pool.country_password_prefix(value),
            "secret-country-US",
        )

    def test_generated_sessions_are_unique_and_documentation_compatible(self):
        sessions = set()
        rng = random.SystemRandom()
        for _ in range(100):
            generate_zip_pool.make_session(sessions, rng)
        self.assertEqual(len(sessions), 100)
        self.assertTrue(all(len(session) == 8 for session in sessions))
        allowed = set(generate_zip_pool.SESSION_ALPHABET)
        self.assertTrue(all(set(session) <= allowed for session in sessions))

    def test_populates_runtime_environment_without_env_file(self):
        environment = {
            "RAYOBYTE_US_PROXY_SERVER": "http://proxy.test:8000",
            "RAYOBYTE_US_USERNAME": "private-user",
            "RAYOBYTE_US_PASSWORD": "private-password-country-US",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            zip_path = root / "zips.txt"
            proxy_path = root / "proxies.txt"
            zip_path.write_text("75032\n10001\n", encoding="utf-8")

            distribution = generate_zip_pool.populate_runtime_pool(
                environment, zip_path, proxy_path, 4
            )

            generated = [
                value
                for key, value in environment.items()
                if key.startswith("RAYOBYTE_US_POOL_PASSWORD_")
            ]
            self.assertEqual(len(generated), 4)
            self.assertEqual(sum(distribution.values()), 4)
            self.assertTrue(all("-zip-" in value for value in generated))
            self.assertTrue(all("-session-" in value for value in generated))
            self.assertTrue(all("-asn-" not in value for value in generated))
            self.assertEqual(len(set(generated)), 4)
            self.assertEqual(len(proxy_path.read_text().splitlines()), 5)

    def test_writes_secrets_only_to_env_and_references_to_csv(self):
        profiles = [("75032", "a1b2c3d4"), ("10001", "e5f6g7h8")]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            env_path = root / ".env"
            proxy_path = root / "proxies.txt"
            env_path.write_text("BASE=1\n", encoding="utf-8")

            generate_zip_pool.write_env_pool(
                env_path,
                env_path.read_text(encoding="utf-8"),
                "private-country-US",
                profiles,
            )
            generate_zip_pool.write_proxy_list(proxy_path, profiles)

            env_content = env_path.read_text(encoding="utf-8")
            proxy_content = proxy_path.read_text(encoding="utf-8")
            self.assertIn("private-country-US-zip-75032-session-a1b2c3d4", env_content)
            self.assertNotIn("private-country-US", proxy_content)
            self.assertNotIn("asn", env_content.lower())
            self.assertIn("RAYOBYTE_US_POOL_PASSWORD_001", proxy_content)


if __name__ == "__main__":
    unittest.main()
