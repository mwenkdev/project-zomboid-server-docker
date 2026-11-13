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


PARENT_XML = "<animNode><m_Name>Parent</m_Name></animNode>"
CHILD_XML = '<animNode x_extends="LS2HDefault.xml"><m_Name>Child</m_Name></animNode>'


class ModFolderAliasTests(unittest.TestCase):
    """A mod folder gets a lowercase alias only when its animation XML inherits."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.item = self.root / "3403870858"
        self.mods = self.item / "mods"

    def tearDown(self):
        for directory, dirs, _ in os.walk(self.root):
            for name in dirs:
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
        self._temp.cleanup()

    def inheriting_mod(self, folder="Lifestyle", version="common", attribute="x_extends"):
        anims = self.mods / folder / version / "media/AnimSets/player/melee/2handed"
        write(self.mods / folder / version / "mod.info", f"id={folder}Id\n")
        write(anims / "LS2HDefault.xml", PARENT_XML)
        write(anims / "LS2HDefaultCrit.xml", CHILD_XML.replace("x_extends", attribute))
        return anims

    def test_x_extends_gets_mod_folder_alias_and_lowercased_parent_path_resolves(self):
        self.inheriting_mod()
        result = fix_mod_case.fix_tree(self.item)
        self.assertEqual(os.readlink(self.mods / "lifestyle"), "Lifestyle")
        # The exact path the game builds for the x_extends parent.
        parent = self.mods / "lifestyle/common/media/animsets/player/melee/2handed/ls2hdefault.xml"
        self.assertEqual(parent.read_text(), PARENT_XML)
        self.assertEqual(result.mod_folder_aliases, ["mods/lifestyle -> Lifestyle"])
        self.assertIn("Lifestyle", result.mods)
        self.assertEqual(result.collisions, [])
        self.assertEqual(result.errors, [])

    def test_x_include_and_actiongroups_in_a_version_folder_are_detected(self):
        write(self.mods / "MoreTraits/42.0/mod.info", "id=moreTraits\n")
        write(self.mods / "MoreTraits/42.0/media/actiongroups/player/Hit.xml",
              '<actiongroup x_include="Base.xml"/>')
        fix_mod_case.fix_tree(self.item)
        self.assertEqual(os.readlink(self.mods / "moretraits"), "MoreTraits")

    def test_mod_folders_without_animation_inheritance_get_no_alias(self):
        write(self.mods / "PlainMod/42/media/AnimSets/player/Idle.xml", PARENT_XML)
        write(self.mods / "PlainMod/42/media/clothing/Shirt.xml", CHILD_XML)  # not animation XML
        write(self.mods / "PlainMod/42/media/AnimSets/Readme.txt", "x_extends=")  # not XML
        result = fix_mod_case.fix_tree(self.item)
        self.assertFalse(os.path.lexists(self.mods / "plainmod"))
        self.assertEqual(result.mod_folder_aliases, [])
        # Everything inside the mod is still aliased as before.
        self.assertEqual(os.readlink(self.mods / "PlainMod/42/media/animsets"), "AnimSets")

    def test_lowercase_mod_folder_needs_no_alias(self):
        self.inheriting_mod(folder="tsarslib")
        result = fix_mod_case.fix_tree(self.item)
        self.assertEqual(sorted(os.listdir(self.mods)), ["tsarslib"])
        self.assertEqual(result.mod_folder_aliases, [])

    def test_inheritance_reached_only_through_a_symlink_does_not_count(self):
        outside = self.root / "outside/AnimSets"
        write(outside / "Child.xml", CHILD_XML)
        media = self.mods / "LinkedMod/42/media"
        media.mkdir(parents=True)
        os.symlink(outside, media / "AnimSets")
        self.assertFalse(fix_mod_case.uses_xml_inheritance(self.mods / "LinkedMod"))
        fix_mod_case.fix_tree(self.item)
        self.assertFalse(os.path.lexists(self.mods / "linkedmod"))

    def test_repeated_runs_are_idempotent_and_recreate_a_removed_alias(self):
        self.inheriting_mod()
        first = fix_mod_case.fix_tree(self.item)
        before = snapshot(self.root)
        second = fix_mod_case.fix_tree(self.item)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.existing, first.created)
        self.assertEqual(second.mod_folder_aliases, [])
        os.unlink(self.mods / "lifestyle")  # e.g. removed by a SteamCMD update
        third = fix_mod_case.fix_tree(self.item)
        self.assertEqual(third.mod_folder_aliases, ["mods/lifestyle -> Lifestyle"])
        self.assertEqual(snapshot(self.root), before)

    def test_existing_alias_is_kept_even_if_the_mod_stops_inheriting(self):
        anims = self.inheriting_mod()
        fix_mod_case.fix_tree(self.item)
        (anims / "LS2HDefaultCrit.xml").write_text(PARENT_XML)
        result = fix_mod_case.fix_tree(self.item)
        self.assertEqual(os.readlink(self.mods / "lifestyle"), "Lifestyle")
        self.assertEqual(result.collisions, [])

    def test_real_lowercase_mod_folder_is_a_collision_and_untouched(self):
        self.inheriting_mod()
        write(self.mods / "lifestyle/common/mod.info", "id=Other\n")
        result = fix_mod_case.fix_tree(self.item)
        self.assertFalse((self.mods / "lifestyle").is_symlink())
        self.assertEqual((self.mods / "lifestyle/common/mod.info").read_text(), "id=Other\n")
        self.assertEqual(len([c for c in result.collisions
                              if "mods/lifestyle'" in c and "both exist" in c]), 1)
        self.assertEqual(result.mod_folder_aliases, [])

    def test_case_variant_mod_folders_are_a_collision(self):
        self.inheriting_mod("Lifestyle")
        self.inheriting_mod("LIFESTYLE")
        result = fix_mod_case.fix_tree(self.item)
        self.assertFalse(os.path.lexists(self.mods / "lifestyle"))
        self.assertTrue(any("all lowercase to 'lifestyle'" in c for c in result.collisions))

    def test_foreign_or_broken_symlink_at_alias_name_is_kept(self):
        self.inheriting_mod()
        os.symlink("Somewhere", self.mods / "lifestyle")
        result = fix_mod_case.fix_tree(self.item)
        self.assertEqual(os.readlink(self.mods / "lifestyle"), "Somewhere")
        self.assertTrue(any("broken symlink" in c for c in result.collisions))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_read_only_mods_folder_is_reported(self):
        self.inheriting_mod()
        os.chmod(self.mods, 0o555)
        result = fix_mod_case.fix_tree(self.item)
        self.assertFalse(os.path.lexists(self.mods / "lifestyle"))
        self.assertTrue(any("cannot create" in e and "lifestyle" in e for e in result.errors))

    def test_main_logs_the_mod_folder_alias(self):
        self.inheriting_mod()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fix_mod_case.main([str(self.root)])
        self.assertIn("mod folder alias 'mods/lifestyle -> Lifestyle'", output.getvalue())

    def test_resolver_still_sees_one_mod_and_its_maps(self):
        from scripts import resolve_mods
        self.inheriting_mod(version="42")
        write(self.mods / "Lifestyle/42/media/maps/Some Map/map.info")
        before = resolve_mods.discover(self.item)
        fix_mod_case.fix_tree(self.item)
        self.assertTrue((self.mods / "lifestyle").is_symlink())
        self.assertEqual(resolve_mods.discover(self.item), before)
        self.assertEqual(before, (["LifestyleId"], ["Some Map"]))
        self.assertEqual(resolve_mods.map_sources(self.item, "Some Map"),
                         [self.mods / "Lifestyle/42/media/maps/Some Map"])
        self.assertTrue(resolve_mods.installed(self.item))


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
