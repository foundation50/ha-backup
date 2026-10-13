import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "rootfs/usr/bin/github-backup"
loader = importlib.machinery.SourceFileLoader("github_backup", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
backup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backup
loader.exec_module(backup)


class CronScheduleTests(unittest.TestCase):
    def test_matches_minute_step_and_weekday(self) -> None:
        schedule = backup.CronSchedule("*/30 3 * * 2")

        self.assertTrue(schedule.matches(datetime(2026, 10, 13, 3, 30)))
        self.assertFalse(schedule.matches(datetime(2026, 10, 13, 3, 31)))
        self.assertFalse(schedule.matches(datetime(2026, 10, 14, 3, 30)))

    def test_uses_cron_or_for_restricted_day_fields(self) -> None:
        schedule = backup.CronSchedule("0 3 14 * 2")

        self.assertTrue(schedule.matches(datetime(2026, 10, 13, 3, 0)))
        self.assertTrue(schedule.matches(datetime(2026, 10, 14, 3, 0)))
        self.assertFalse(schedule.matches(datetime(2026, 10, 15, 3, 0)))


class FileCollectionTests(unittest.TestCase):
    def test_collects_files_once_and_skips_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "config"
            nested = source / "nested"
            nested.mkdir(parents=True)
            (source / "configuration.yaml").write_text("homeassistant: {}\n")
            (nested / "automation.yaml").write_text("[]\n")
            (source / "linked.yaml").symlink_to(nested / "automation.yaml")

            settings = backup.Settings(
                repository="owner/repository",
                branch="main",
                schedule="0 3 * * *",
                sources=(source,),
                backup_path="backup",
                run_on_startup=True,
                token="token",
            )

            self.assertEqual(
                backup.collect_files(settings),
                {
                    f"backup{source}/configuration.yaml": source / "configuration.yaml",
                    f"backup{source}/nested/automation.yaml": nested / "automation.yaml",
                },
            )


class SettingsTests(unittest.TestCase):
    def test_rejects_non_string_option_token(self) -> None:
        original_options_path = backup.OPTIONS_PATH
        original_allowed_roots = backup.ALLOWED_ROOTS
        previous_token = os.environ.pop("GITHUB_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                options_path = Path(temporary_directory) / "options.json"
                source = Path(temporary_directory) / "config.yaml"
                source.write_text("homeassistant: {}\n")
                backup.ALLOWED_ROOTS = (source.parent.resolve(),)
                options_path.write_text(
                    (
                        '{"repository":"owner/repository","branch":"main",'
                        '"schedule":"0 3 * * *","whitelist":["'
                        + str(source)
                        + '"],"backup_path":"backup","github_token":null}'
                    )
                )
                backup.OPTIONS_PATH = options_path

                with self.assertRaisesRegex(
                    backup.ConfigurationError, "github_token must be a string"
                ):
                    backup.read_settings()
        finally:
            backup.OPTIONS_PATH = original_options_path
            backup.ALLOWED_ROOTS = original_allowed_roots
            if previous_token is not None:
                os.environ["GITHUB_TOKEN"] = previous_token

    def test_environment_token_takes_precedence(self) -> None:
        previous_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "environment-token"
        original_options_path = backup.OPTIONS_PATH
        original_allowed_roots = backup.ALLOWED_ROOTS
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                options_path = Path(temporary_directory) / "options.json"
                source = Path(temporary_directory) / "config.yaml"
                source.write_text("homeassistant: {}\n")
                backup.ALLOWED_ROOTS = (source.parent.resolve(),)
                options_path.write_text(
                    (
                        '{"repository":"owner/repository","branch":"main",'
                        '"schedule":"0 3 * * *","whitelist":["'
                        + str(source)
                        + '"],"backup_path":"backup","github_token":"option-token"}'
                    )
                )
                backup.OPTIONS_PATH = options_path

                self.assertEqual(backup.read_settings().token, "environment-token")
        finally:
            backup.OPTIONS_PATH = original_options_path
            backup.ALLOWED_ROOTS = original_allowed_roots
            if previous_token is None:
                del os.environ["GITHUB_TOKEN"]
            else:
                os.environ["GITHUB_TOKEN"] = previous_token


class MainTests(unittest.TestCase):
    def test_logs_when_startup_backup_is_disabled(self) -> None:
        settings = backup.Settings(
            repository="owner/repository",
            branch="main",
            schedule="0 0 1 1 *",
            sources=(),
            backup_path="backup",
            run_on_startup=False,
            token="token",
        )

        with (
            mock.patch.object(backup, "read_settings", return_value=settings),
            mock.patch.object(backup.time, "sleep", side_effect=KeyboardInterrupt),
            self.assertLogs(backup.LOG, level="INFO") as logs,
        ):
            with self.assertRaises(KeyboardInterrupt):
                backup.main()

        self.assertTrue(
            any(
                "Backup service started for owner/repository@main; schedule: 0 0 1 1 *; startup backup: disabled"
                in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any(
                "Startup backup disabled; waiting for the next scheduled backup" in message
                for message in logs.output
            )
        )


class SyncTests(unittest.TestCase):
    def test_sync_creates_atomic_commit_and_removes_stale_manifest_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "config.yaml"
            source.write_text("new: value\n")
            settings = backup.Settings(
                repository="owner/repository",
                branch="main",
                schedule="0 3 * * *",
                sources=(source,),
                backup_path="backup",
                run_on_startup=True,
                token="token",
            )
            calls = []

            class FakeClient:
                def __init__(self, repository: str, token: str) -> None:
                    self.repository = repository
                    self.token = token

                def request(self, method: str, path: str, payload=None):
                    calls.append((method, path, payload))
                    responses = {
                        ("GET", "/git/ref/heads/main"): {"object": {"sha": "old-commit"}},
                        ("GET", "/git/commits/old-commit"): {"tree": {"sha": "old-tree"}},
                        ("GET", "/git/trees/old-tree?recursive=1"): {
                            "tree": [{"path": "backup/.ha-github-backup-manifest.json", "sha": "manifest"}]
                        },
                        ("GET", "/git/blobs/manifest"): {
                            "content": "WyJiYWNrdXAvcmVtb3ZlZC55YW1sIl0="
                        },
                        ("POST", "/git/blobs"): {"sha": f"blob-{len(calls)}"},
                        ("POST", "/git/trees"): {"sha": "new-tree"},
                        ("POST", "/git/commits"): {"sha": "new-commit"},
                        ("PATCH", "/git/refs/heads/main"): {},
                    }
                    return responses[(method, path)]

            original_client = backup.GitHubClient
            backup.GitHubClient = FakeClient
            try:
                backup.sync(settings)
            finally:
                backup.GitHubClient = original_client

            tree_payload = next(payload for method, path, payload in calls if path == "/git/trees")
            self.assertIn(
                {"path": "backup/removed.yaml", "mode": "100644", "type": "blob", "sha": None},
                tree_payload["tree"],
            )
            self.assertEqual(
                calls[-1],
                ("PATCH", "/git/refs/heads/main", {"sha": "new-commit", "force": False}),
            )


if __name__ == "__main__":
    unittest.main()
