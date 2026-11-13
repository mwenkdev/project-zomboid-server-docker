#!/usr/bin/env python3
"""Add lowercase symlink aliases to Workshop mods with mixed-case paths.

Mods are written on case-insensitive Windows filesystems. Some reference a path
with a different case than the shipped file (for example the XML of Tsar's
Common Library asks for media/animsets while the folder is media/AnimSets), and
that fails on Linux. For every file or folder name containing uppercase
characters this creates a sibling symlink with the lowercase name, pointing at
the original by its bare name. Aliases at every level compose, so a fully
lowercased path resolves through the original files.

Safety rules:
- Original files and folders are never modified, renamed or deleted.
- Nothing that already exists (file, folder, symlink or broken symlink) is
  overwritten. Matching aliases from a previous run are counted as existing,
  so the script is idempotent and recreates aliases an update removed.
- Symlinks are never followed or aliased, so loops and the new aliases are
  never traversed.
- Names that lowercase to the same alias (Foo and FOO) are reported and skipped.

Some subtrees are deliberately left alone because the game, or this image,
enumerates every entry in them, so an alias would be a duplicate:
- media/lua: the game loads every Lua file it finds. A duplicate would run
  twice and change the server's Lua checksum, which clients must match.
- media/scripts: every script file is parsed, so duplicate definitions appear.
- media/maps: map folders are listed by resolve_mods.py and search_folder.sh.
Folders with these names can still get an alias themselves (Lua -> lua); only
their contents are skipped.

Mod folders (directly under <item>/mods) get an alias only when the game
needs one. An animation XML (media/AnimSets or media/actiongroups) that uses
x_extends or x_include makes the game open its parent file through the whole
path lowercased, mod folder included (ZomboidFileSystem.getRelativeFile), so
mods/Lifestyle/... is looked up as mods/lifestyle/.... Other mod folders are
left alone because the game lists every folder under mods/: an alias is seen
as a second folder for the same mod ID. The game keeps one folder per mod ID
and loads each ID once, so the duplicate is not loaded twice, but there is no
reason to add one that nothing needs.
"""

import argparse
from dataclasses import dataclass, field
import os
import re
import sys


SKIPPED_MEDIA_SUBTREES = frozenset({"lua", "scripts", "maps"})
ANIMATION_XML_DIRS = frozenset({"animsets", "actiongroups"})
XML_INHERITANCE = re.compile(rb"\bx_(?:extends|include)\s*=", re.IGNORECASE)
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_REPORTED = 20


@dataclass
class Result:
    created: int = 0
    existing: int = 0
    collisions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    mods: set = field(default_factory=set)
    mod_folder_aliases: list = field(default_factory=list)


def _show(path):
    # repr escapes newlines and undecodable bytes, so any name can be logged.
    return repr(os.fsdecode(path))


def _skip_subtree(rel, name):
    return bool(rel) and rel[-1].lower() == "media" and name.lower() in SKIPPED_MEDIA_SUBTREES


def _mod_name(rel):
    return rel[1] if len(rel) >= 2 and rel[0] == "mods" else "(item root)"


def uses_xml_inheritance(mod_dir):
    """True if an animation XML in mod_dir uses x_extends or x_include.

    Only real files below a media/AnimSets or media/actiongroups folder (any
    case, any version folder) count. Symlinks, including aliases made by an
    earlier run, are neither read nor followed. Unreadable files are skipped.
    """
    for directory, dirs, files in os.walk(mod_dir, followlinks=False):
        parts = [part.lower() for part in os.path.relpath(directory, mod_dir).split(os.sep)]
        in_animation_dir = any(
            parts[i] == "media" and parts[i + 1] in ANIMATION_XML_DIRS for i in range(len(parts) - 1)
        )
        if not in_animation_dir:
            continue
        for name in files:
            path = os.path.join(directory, name)
            if not name.lower().endswith(".xml") or os.path.islink(path):
                continue
            try:
                with open(path, "rb") as handle:
                    if XML_INHERITANCE.search(handle.read(MAX_XML_BYTES)):
                        return True
            except OSError:
                continue
    return False


def _keep_mod_folder_alias(directory, lower, originals, by_name):
    """Decide whether a mod folder group should be handled at all."""
    existing = by_name.get(lower)
    if existing is not None and existing.is_symlink():
        try:
            if os.readlink(existing.path) in originals:
                return True  # our alias from an earlier run: count it as present
        except OSError:
            pass
    return any(uses_xml_inheritance(os.path.join(directory, name)) for name in originals)


def _check_existing(path, original, result):
    """Handle an alias name that is already taken. Never replaces it."""
    try:
        if os.path.islink(path):
            target = os.readlink(path)
            if target == original:
                result.existing += 1
                return
            state = "a broken symlink" if not os.path.exists(path) else "a symlink"
            result.collisions.append(
                f"{_show(path)} is {state} to {_show(target)}, not to {_show(original)}; left unchanged"
            )
        else:
            result.collisions.append(
                f"{_show(path)} and {_show(original)} both exist as real entries; "
                "the lowercase one is left in place"
            )
    except OSError as error:
        result.errors.append(f"cannot inspect {_show(path)}: {error.strerror or error}")


def fix_tree(item_root, result=None):
    """Create lowercase aliases below one Workshop item folder."""
    result = result if result is not None else Result()
    pending: list[tuple[str, tuple[str, ...]]] = [(os.fspath(item_root), ())]
    while pending:
        directory, rel = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                # The listing is taken before any alias is created, so aliases
                # made in this directory are never visited.
                entries = list(iterator)
        except OSError as error:
            result.errors.append(f"cannot list {_show(directory)}: {error.strerror or error}")
            continue

        by_name = {entry.name: entry for entry in entries}
        aliases = {}
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as error:
                result.errors.append(f"cannot inspect {_show(entry.path)}: {error.strerror or error}")
                continue
            lower = entry.name.lower()
            if lower != entry.name and (rel != ("mods",) or is_dir):
                aliases.setdefault(lower, []).append(entry.name)
            if is_dir and not _skip_subtree(rel, entry.name):
                pending.append((entry.path, rel + (entry.name,)))

        if rel == ("mods",):
            # Mod folders only get an alias when their animation XML needs it.
            aliases = {lower: names for lower, names in aliases.items()
                       if _keep_mod_folder_alias(directory, lower, names, by_name)}

        for lower in sorted(aliases):
            originals = sorted(aliases[lower])
            path = os.path.join(directory, lower)
            if len(originals) > 1:
                result.collisions.append(
                    f"{', '.join(_show(name) for name in originals)} in {_show(directory)} "
                    f"all lowercase to {_show(lower)}; no alias created"
                )
                continue
            original = originals[0]
            if lower in by_name:
                _check_existing(path, original, result)
                continue
            try:
                os.symlink(original, path)
            except FileExistsError:
                _check_existing(path, original, result)
            except OSError as error:
                result.errors.append(f"cannot create {_show(path)}: {error.strerror or error}")
            else:
                result.created += 1
                if rel == ("mods",):
                    result.mods.add(original)
                    result.mod_folder_aliases.append(f"mods/{lower} -> {original}")
                else:
                    result.mods.add(_mod_name(rel))
    return result


def fix_workshop(content_dir):
    """Fix every Workshop item folder in content_dir; return {item: Result}."""
    results = {}
    with os.scandir(content_dir) as iterator:
        items = sorted(iterator, key=lambda entry: entry.name)
    for item in items:
        if item.is_symlink() or not item.is_dir(follow_symlinks=False):
            continue
        results[item.name] = fix_tree(item.path)
    return results


def _report(label, lines):
    for line in lines[:MAX_REPORTED]:
        print(f"Mod case fix: {label}: {line}", flush=True)
    if len(lines) > MAX_REPORTED:
        print(f"Mod case fix: {label}: ... and {len(lines) - MAX_REPORTED} more", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add lowercase symlink aliases to Workshop mods.")
    parser.add_argument("content_dir", help="steamapps/workshop/content/108600")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.content_dir):
        print(f"Mod case fix: no Workshop content at {_show(args.content_dir)}; nothing to do", flush=True)
        return 0
    try:
        results = fix_workshop(args.content_dir)
    except OSError as error:
        print(f"Mod case fix: cannot list {_show(args.content_dir)}: {error}", file=sys.stderr)
        return 1

    for item, result in results.items():
        if result.created:
            mods = ", ".join(sorted(result.mods))
            print(f"Mod case fix: Workshop item {item} ({mods}): created {result.created} "
                  f"aliases, {result.existing} already present", flush=True)
        for alias in result.mod_folder_aliases:
            print(f"Mod case fix: Workshop item {item}: mod folder alias {_show(alias)} "
                  "(animation XML uses x_extends/x_include)", flush=True)
        _report(f"WARNING: collision in {item}", result.collisions)
        _report(f"WARNING: error in {item}", result.errors)

    values = results.values()
    print(f"Mod case fix: {len(results)} Workshop items scanned; "
          f"{sum(r.created for r in values)} aliases created, "
          f"{sum(r.existing for r in values)} already present, "
          f"{sum(len(r.collisions) for r in values)} collisions, "
          f"{sum(len(r.errors) for r in values)} errors", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
