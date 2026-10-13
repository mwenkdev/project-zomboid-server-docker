import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import resolve_mods


class ResolverTests(unittest.TestCase):
    def test_resolves_installed_mod_and_map_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "install"
            item = install / "steamapps/workshop/content/108600/123"
            mod = item / "mods/Example"
            map_folder = mod / "media/maps/ExampleMap"
            map_folder.mkdir(parents=True)
            (mod / "mod.info").write_text("name=Example\nid=ExampleMod\n")
            (map_folder / "map.info").write_text("title=ExampleMap\n")
            ini = root / "Server/servertest.ini"
            ini.parent.mkdir()
            ini.write_text("PublicName=Unchanged\nPassword=secret\nMap=OldMap;Muldraugh, KY\n")
            manifest = root / "mods.json"
            manifest.write_text(json.dumps([{"workshopId": "123"}]))
            with patch.object(resolve_mods, "prepare_workshop_items"), patch("sys.argv", [
                "resolve_mods.py", "--manifest", str(manifest), "--ini", str(ini),
                "--steamcmd", str(root), "--install", str(install),
            ]):
                resolve_mods.main()
            self.assertEqual(ini.read_text(),
                "PublicName=Unchanged\nPassword=secret\nMap=ExampleMap;Muldraugh, KY\n"
                "WorkshopItems=123\nMods=ExampleMod\n")
            self.assertTrue((install / "media/maps/ExampleMap/map.info").exists())

    def test_refresh_uses_one_steamcmd_session_for_all_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workshop = root / "install/steamapps/workshop/content/108600"
            for item_id in ("123", "456"):
                folder = workshop / item_id
                folder.mkdir(parents=True)
                (folder / "mod.info").write_text("id=Example\n")
            with patch.object(resolve_mods.subprocess, "run") as run:
                resolve_mods.prepare_workshop_items(
                    ["123", "456"], root / "steamcmd", root / "install", workshop, True
                )
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertEqual(args.count("+login"), 1)
            self.assertEqual(args.count("+workshop_download_item"), 2)
            self.assertEqual(args[-1], "+quit")

    def test_cached_items_skip_refresh_but_missing_items_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workshop = root / "install/steamapps/workshop/content/108600"
            cached = workshop / "123"
            cached.mkdir(parents=True)
            (cached / "mod.info").write_text("id=Cached\n")
            with patch.object(resolve_mods.subprocess, "run") as run:
                resolve_mods.prepare_workshop_items(
                    ["123"], root / "steamcmd", root / "install", workshop, False
                )
            run.assert_not_called()

            def create_missing(args, check):
                missing = workshop / "456"
                missing.mkdir()
                (missing / "mod.info").write_text("id=New\n")

            with patch.object(resolve_mods.subprocess, "run", side_effect=create_missing) as run:
                resolve_mods.prepare_workshop_items(
                    ["123", "456"], root / "steamcmd", root / "install", workshop, False
                )
            args = run.call_args.args[0]
            self.assertEqual(args.count("+workshop_download_item"), 1)
            self.assertIn("456", args)
            self.assertNotIn("123", args)

    def test_update_setting_rejects_invalid_values(self):
        with patch.dict(os.environ, {"MOD_UPDATE_ON_START": "sometimes"}):
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                resolve_mods.update_on_start()

    def test_ambiguous_mod_ids_require_explicit_selection(self):
        with self.assertRaisesRegex(ValueError, "select explicitly"):
            resolve_mods.select(None, ["B41", "B42"], "modIds", "123")

    def test_duplicate_ini_keys_fail_before_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "server.ini"
            ini.write_text("Mods=first\nMods=second\n")
            with self.assertRaisesRegex(ValueError, "Duplicate Mods"):
                resolve_mods.update_ini(ini, {"Mods": "third"})
            self.assertEqual(ini.read_text(), "Mods=first\nMods=second\n")

    def test_common_and_b42_map_layers_are_applied_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = root / "mods/RV_Interior/common/media/maps/interior"
            b42 = root / "mods/RV_Interior/42/media/maps/interior"
            common.mkdir(parents=True)
            b42.mkdir(parents=True)
            (common / "map.info").write_text("common")
            (b42 / "map.info").write_text("b42")
            sources = resolve_mods.map_sources(root, "interior")
            self.assertEqual(sources, [common, b42])
            destination = root / "installed/interior"
            from shutil import copytree
            for source in sources:
                copytree(source, destination, dirs_exist_ok=True)
            self.assertEqual((destination / "map.info").read_text(), "b42")

    def test_unrelated_duplicate_map_sources_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mods/A/media/maps/interior").mkdir(parents=True)
            (root / "mods/B/media/maps/interior").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "unambiguous source"):
                resolve_mods.map_sources(root, "interior")


if __name__ == "__main__":
    unittest.main()
