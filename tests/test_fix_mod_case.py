import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import fix_mod_case


def write(path, text="data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def snapshot(root):
    """Every path below root with its type, link target or content."""
    state = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                state[path] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                state[path] = ("dir",)
            else:
                state[path] = ("file", Path(path).read_bytes())
    return state


class FixModCaseTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.mod = self.root / "3402491515/mods/tsarslib"

    def tearDown(self):
        for directory, dirs, _ in os.walk(self.root):
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
        self._temp.cleanup()

    def run_fix(self):
        return fix_mod_case.fix_tree(self.root / "3402491515")

    def test_tsarslib_animsets_resolves_through_lowercase_path(self):
        original = write(self.mod / "media/AnimSets/player-vehicle/aim/Aim.xml", "<xml/>")
        result = self.run_fix()
        alias = self.mod / "media/animsets"
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), "AnimSets")
        self.assertEqual((alias / "player-vehicle/aim/aim.xml").read_text(), "<xml/>")
        self.assertEqual(original.read_text(), "<xml/>")
        self.assertFalse(original.is_symlink())
        self.assertEqual(result.created, 2)
        self.assertEqual(result.mods, {"tsarslib"})
        self.assertEqual(result.collisions, [])
        self.assertEqual(result.errors, [])

    def test_nested_mixed_case_paths_compose(self):
        write(self.mod / "media/Textures/UI/Icons/My_Icon.PNG", "png")
        self.run_fix()
        self.assertEqual((self.mod / "media/textures/ui/icons/my_icon.png").read_text(), "png")
        # The original spelling and mixed spellings keep working.
        self.assertEqual((self.mod / "media/Textures/UI/Icons/My_Icon.PNG").read_text(), "png")
        self.assertEqual((self.mod / "media/textures/UI/icons/My_Icon.PNG").read_text(), "png")

    def test_repeated_runs_are_idempotent(self):
        write(self.mod / "media/AnimSets/Player/Idle.xml")
        first = self.run_fix()
        before = snapshot(self.root)
        second = self.run_fix()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.existing, first.created)
        self.assertEqual(second.collisions, [])

    def test_missing_alias_is_recreated(self):
        write(self.mod / "media/AnimSets/x.xml")
        self.run_fix()
        os.unlink(self.mod / "media/animsets")  # e.g. removed by a SteamCMD update
        result = self.run_fix()
        self.assertEqual(result.created, 1)
        self.assertTrue((self.mod / "media/animsets/x.xml").exists())

    def test_existing_lowercase_target_is_not_replaced(self):
        write(self.mod / "media/Sounds/a.ogg", "upper")
        write(self.mod / "media/sounds/b.ogg", "lower")
        result = self.run_fix()
        self.assertFalse((self.mod / "media/sounds").is_symlink())
        self.assertEqual(sorted(os.listdir(self.mod / "media/sounds")), ["b.ogg"])
        self.assertEqual(len(result.collisions), 1)
        self.assertIn("both exist", result.collisions[0])

    def test_conflicting_case_variants_get_no_alias(self):
        write(self.mod / "media/Foo.txt", "one")
        write(self.mod / "media/FOO.txt", "two")
        result = self.run_fix()
        self.assertFalse(os.path.lexists(self.mod / "media/foo.txt"))
        self.assertEqual(result.created, 0)
        self.assertEqual(len(result.collisions), 1)
        self.assertIn("all lowercase to", result.collisions[0])

    def test_spaces_and_unusual_names(self):
        write(self.mod / "media/My Folder/Some File (v2).TXT", "spaced")
        write(self.mod / "media/Line\nBreak.TXT", "newline")
        raw = os.fsencode(self.mod / "media") + b"/Bad\xffName.TXT"
        with open(raw, "w") as handle:
            handle.write("bytes")
        result = self.run_fix()
        self.assertEqual((self.mod / "media/my folder/some file (v2).txt").read_text(), "spaced")
        self.assertEqual((self.mod / "media/line\nbreak.txt").read_text(), "newline")
        with open(os.fsencode(self.mod / "media") + b"/bad\xffname.txt") as handle:
            self.assertEqual(handle.read(), "bytes")
        self.assertEqual(result.errors, [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fix_mod_case.main([str(self.root)])  # logging must not choke on the names
        self.assertIn("0 aliases created", output.getvalue())

    def test_symlinks_are_neither_followed_nor_aliased(self):
        outside = write(self.root / "outside/Secret/File.txt")
        media = self.mod / "media"
        media.mkdir(parents=True)
        os.symlink(outside.parent.parent, media / "External")
        os.symlink(".", media / "Loop")
        os.symlink("Missing", media / "Broken")
        result = self.run_fix()
        self.assertEqual(result.created, 0)
        self.assertFalse(os.path.lexists(media / "external"))
        self.assertFalse(os.path.lexists(media / "loop"))
        self.assertFalse(os.path.lexists(media / "broken"))
        self.assertFalse(os.path.lexists(self.root / "outside/secret"))

    def test_existing_or_broken_symlink_at_alias_name_is_kept(self):
        media = self.mod / "media"
        write(media / "AnimSets/x.xml")
        write(media / "Models/y.fbx")
        os.symlink("Gone", media / "animsets")  # stale alias from an older layout
        os.symlink("elsewhere", media / "models")
        result = self.run_fix()
        self.assertEqual(os.readlink(media / "animsets"), "Gone")
        self.assertEqual(os.readlink(media / "models"), "elsewhere")
        self.assertEqual(result.created, 0)
        self.assertEqual(len(result.collisions), 2)
        self.assertTrue(any("broken symlink" in line for line in result.collisions))

    def test_enumerated_subtrees_and_mod_folders_are_skipped(self):
        root = self.root / "3402491515/mods/SapphCooking/42"
        write(root / "media/lua/client/Cooking/UI.lua")
        write(root / "media/scripts/Items/Food.txt")
        write(root / "media/maps/My Map/map.info")
        write(root / "media/Lua/shared/Foo.lua")
        result = self.run_fix()
        self.assertFalse(os.path.lexists(self.root / "3402491515/mods/sapphcooking"))
        self.assertEqual(sorted(os.listdir(root / "media/lua/client")), ["Cooking"])
        self.assertEqual(sorted(os.listdir(root / "media/scripts")), ["Items"])
        self.assertEqual(sorted(os.listdir(root / "media/maps")), ["My Map"])
        self.assertEqual(os.listdir(root / "media/Lua/shared"), ["Foo.lua"])
        self.assertFalse((root / "media/lua").is_symlink())
        self.assertEqual(result.created, 0)
        self.assertEqual(len(result.collisions), 1)  # media/Lua next to media/lua

    def test_misnamed_skipped_folder_gets_alias_but_contents_do_not(self):
        write(self.mod / "media/Lua/client/Foo.lua")
        result = self.run_fix()
        self.assertEqual(os.readlink(self.mod / "media/lua"), "Lua")
        self.assertEqual(os.listdir(self.mod / "media/Lua/client"), ["Foo.lua"])
        self.assertEqual(result.created, 1)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_permission_failures_are_reported_and_scan_continues(self):
        read_only = write(self.mod / "media/ReadOnly/File.txt").parent
        unreadable = write(self.mod / "media/Hidden/x.txt").parent
        write(self.mod / "media/Other/Good.txt")
        os.chmod(read_only, 0o555)
        os.chmod(unreadable, 0o000)
        result = self.run_fix()
        self.assertFalse(os.path.lexists(read_only / "file.txt"))
        self.assertTrue((self.mod / "media/other/good.txt").exists())
        self.assertTrue(any("cannot create" in line for line in result.errors))
        self.assertTrue(any("cannot list" in line for line in result.errors))

    def test_main_reports_per_item_and_tolerates_missing_directory(self):
        write(self.mod / "media/AnimSets/x.xml")
        write(self.root / "999/mods/clean/media/ok.txt")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(fix_mod_case.main([str(self.root)]), 0)
            self.assertEqual(fix_mod_case.main([str(self.root / "missing")]), 0)
        text = output.getvalue()
        self.assertIn("Workshop item 3402491515 (tsarslib): created 1 aliases", text)
        self.assertNotIn("Workshop item 999", text)
        self.assertIn("2 Workshop items scanned; 1 aliases created", text)
        self.assertIn("nothing to do", text)


class ResolverCompatibilityTests(unittest.TestCase):
    def test_aliases_do_not_change_discovered_mods_or_maps(self):
        from scripts import resolve_mods
        with tempfile.TemporaryDirectory() as directory:
            item = Path(directory) / "123"
            mod = item / "mods/ExampleMod/42"
            write(mod / "mod.info", "id=ExampleMod\n")
            write(mod / "media/maps/Example Map/map.info")
            write(mod / "media/AnimSets/Player/Idle.xml")
            before = resolve_mods.discover(item)
            fix_mod_case.fix_tree(item)
            self.assertEqual(resolve_mods.discover(item), before)
            self.assertEqual(before, (["ExampleMod"], ["Example Map"]))
            self.assertEqual(resolve_mods.map_sources(item, "Example Map"),
                             [mod / "media/maps/Example Map"])


class EntrypointTests(unittest.TestCase):
    def test_case_fix_runs_after_mod_resolution_and_before_server_start(self):
        script = Path(__file__).parent.parent / "scripts/entry.sh"
        text = script.read_text()
        resolve = text.index("resolve_mods.py")
        fix = text.index("fix_mod_case.py")
        start = text.index("./start-server.sh")
        self.assertLess(resolve, fix)
        self.assertLess(fix, start)
        subprocess.run(["bash", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
