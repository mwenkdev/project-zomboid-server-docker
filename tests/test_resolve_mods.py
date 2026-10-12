import json
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
            with patch.object(resolve_mods, "download", return_value=item), patch("sys.argv", [
                "resolve_mods.py", "--manifest", str(manifest), "--ini", str(ini),
                "--steamcmd", str(root), "--install", str(install),
            ]):
                resolve_mods.main()
            self.assertEqual(ini.read_text(),
                "PublicName=Unchanged\nPassword=secret\nMap=ExampleMap;Muldraugh, KY\n"
                "WorkshopItems=123\nMods=ExampleMod\n")
            self.assertTrue((install / "media/maps/ExampleMap/map.info").exists())

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
