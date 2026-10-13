import importlib.machinery
import importlib.util
import json
import os
import subprocess
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
    def test_apply_backup_files_removes_only_manifest_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "config.yaml"
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
            worktree = root / "worktree"
            backup_root = worktree / "backup"
            backup_root.mkdir(parents=True)
            (backup_root / "removed.yaml").write_text("remove me\n")
            (backup_root / "unmanaged.yaml").write_text("keep me\n")
            (backup_root / backup.MANIFEST_NAME).write_text('["backup/removed.yaml"]\n')

            files = {"backup/config/config.yaml": source}
            backup.apply_backup_files(settings, files, worktree)

            self.assertFalse((backup_root / "removed.yaml").exists())
            self.assertTrue((backup_root / "unmanaged.yaml").exists())
            self.assertEqual(
                (worktree / "backup/config/config.yaml").read_text(), "new: value\n"
            )
            self.assertEqual(
                json.loads((backup_root / backup.MANIFEST_NAME).read_text()), sorted(files)
            )

    def test_sync_commits_and_pushes_staged_changes(self) -> None:
        settings = backup.Settings(
            repository="owner/repository",
            branch="main",
            schedule="0 3 * * *",
            sources=(),
            backup_path="backup",
            run_on_startup=True,
            token="token",
        )
        commands = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = mock.MagicMock()
            manager.__enter__.return_value = temporary_directory

            def fake_run_git(arguments, worktree, environment, check=True):
                commands.append((arguments, worktree, environment, check))
                return subprocess.CompletedProcess(
                    arguments, 1 if arguments[0] == "diff" else 0
                )

            with (
                mock.patch.object(backup, "collect_files", return_value={}),
                mock.patch.object(backup.tempfile, "TemporaryDirectory", return_value=manager),
                mock.patch.object(backup, "run_git", side_effect=fake_run_git),
            ):
                backup.sync(settings)

        command_arguments = [command[0] for command in commands]
        self.assertEqual(command_arguments[0], ["init"])
        self.assertIn(
            ["remote", "add", "origin", "https://github.com/owner/repository.git"],
            command_arguments,
        )
        self.assertIn(["fetch", "--depth=1", "origin", "main"], command_arguments)
        self.assertIn(["add", "--all", "--", "backup"], command_arguments)
        self.assertTrue(any(arguments[0] == "commit" or "commit" in arguments for arguments in command_arguments))
        self.assertIn(["push", "origin", "HEAD:refs/heads/main"], command_arguments)
        self.assertTrue(all("token" not in " ".join(arguments) for arguments in command_arguments))

    def test_sync_skips_commit_when_backup_is_current(self) -> None:
        settings = backup.Settings(
            repository="owner/repository",
            branch="main",
            schedule="0 3 * * *",
            sources=(),
            backup_path="backup",
            run_on_startup=True,
            token="token",
        )
        commands = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = mock.MagicMock()
            manager.__enter__.return_value = temporary_directory

            def fake_run_git(arguments, worktree, environment, check=True):
                commands.append(arguments)
                return subprocess.CompletedProcess(arguments, 0)

            with (
                mock.patch.object(backup, "collect_files", return_value={}),
                mock.patch.object(backup.tempfile, "TemporaryDirectory", return_value=manager),
                mock.patch.object(backup, "run_git", side_effect=fake_run_git),
                self.assertLogs(backup.LOG, level="INFO") as logs,
            ):
                backup.sync(settings)

        self.assertTrue(any("Backup is already current" in message for message in logs.output))
        self.assertFalse(any("commit" in arguments for arguments in commands))
        self.assertFalse(any(arguments[0] == "push" for arguments in commands))


if __name__ == "__main__":
    unittest.main()
