#!/usr/bin/env python3
"""Resolve a Helm mod manifest against Steam Workshop and installed PZ content."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request


APP_ID = "108600"


def workshop_id(item):
    if item.get("workshopId"):
        value = str(item["workshopId"])
        if not value.isdigit():
            raise ValueError(f"Invalid workshopId: {value!r}")
        return value
    name = item.get("name", "")
    key = os.environ.get("STEAM_WEB_API_KEY", "")
    if not name or not key:
        raise ValueError(f"A mod without workshopId needs a name and STEAM_WEB_API_KEY: {name!r}")
    query = urllib.parse.urlencode({
        "key": key, "query_type": 12, "search_text": name,
        "appid": APP_ID, "creator_appid": APP_ID, "numperpage": 100,
    })
    request = urllib.request.Request(
        "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/?" + query
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        files = json.load(response).get("response", {}).get("publishedfiledetails", [])
    matches = [entry for entry in files if entry.get("title", "").casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(
            f"Workshop name {name!r} has {len(matches)} exact matches in the first 100 results; "
            "specify workshopId instead"
        )
    return str(matches[0]["publishedfileid"])


def download(item_id, steamcmd, install_dir, workshop_dir):
    target = workshop_dir / item_id
    # SteamCMD puts Workshop downloads under its own steamapps directory on some
    # versions, regardless of force_install_dir. Check both locations.
    subprocess.run([
        str(steamcmd / "steamcmd.sh"), "+force_install_dir", str(install_dir),
        "+login", "anonymous", "+workshop_download_item", APP_ID, item_id,
        "validate", "+quit",
    ], check=True)
    source = steamcmd / "steamapps" / "workshop" / "content" / APP_ID / item_id
    if source.is_dir() and source.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    if not target.is_dir():
        raise ValueError(f"Workshop item {item_id} was not installed at {target}")
    return target


def discover(folder):
    mod_ids = []
    maps = []
    for info in sorted(folder.rglob("mod.info")):
        for line in info.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = re.match(r"\s*id\s*=\s*(.*?)\s*$", line, re.I)
            if match and match.group(1) and match.group(1) not in mod_ids:
                mod_ids.append(match.group(1))
        for map_dir in (path for path in info.parent.rglob("maps") if path.parent.name == "media"):
            for child in sorted(map_dir.iterdir()):
                if child.is_dir() and child.name not in maps:
                    maps.append(child.name)
    return mod_ids, maps


def select(requested, found, label, item_id):
    if requested is not None:
        if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
            raise ValueError(f"{label} for {item_id} must be a list of strings")
        missing = set(requested) - set(found)
        if missing:
            raise ValueError(f"{item_id}: unknown {label}: {sorted(missing)}; found {found}")
        selected = requested
    else:
        if len(found) > 1:
            raise ValueError(f"{item_id}: multiple {label} found: {found}; select explicitly")
        selected = found
    if any("\n" in value or "\r" in value or ";" in value for value in selected):
        raise ValueError(f"{item_id}: {label} contains an INI delimiter")
    return selected


def map_sources(folder, map_name):
    sources = [path for path in folder.rglob("media/maps/" + map_name) if path.is_dir()]
    if len(sources) == 1:
        return sources
    # Build 42 items may ship the same map under common/ and 42/. Apply the
    # shared files first and the B42 files second, just as the mod itself does.
    common = [path for path in sources if path.parents[2].name == "common"]
    b42 = [path for path in sources if path.parents[2].name == "42"]
    if len(common) <= 1 and len(b42) == 1 and len(common) + len(b42) == len(sources):
        return common + b42
    raise ValueError(f"expected one unambiguous source for map {map_name!r}; found {sources}")


def update_ini(path, values):
    original = path.read_text() if path.exists() else ""
    lines: list[str] = [str(line) for line in original.splitlines(keepends=True)]
    for key, value in values.items():
        matching = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
        if len(matching) > 1:
            raise ValueError(f"Duplicate {key}= lines in {path}")
        line = f"{key}={value}\n"
        if matching:
            lines[matching[0]] = line
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".mod-tmp")
    temp.write_text("".join(lines))
    if path.exists():
        shutil.copymode(path, temp)
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ini", type=Path, required=True)
    parser.add_argument("--steamcmd", type=Path, required=True)
    parser.add_argument("--install", type=Path, required=True)
    args = parser.parse_args()
    mods = json.loads(args.manifest.read_text())
    if not isinstance(mods, list):
        raise ValueError("Mod manifest must be a JSON list")
    workshop_dir = args.install / "steamapps" / "workshop" / "content" / APP_ID
    ids, mod_ids, maps = [], [], []
    for item in mods:
        if not isinstance(item, dict):
            raise ValueError("Each mod must be an object")
        item_id = workshop_id(item)
        if item_id in ids:
            raise ValueError(f"Duplicate Workshop item {item_id}")
        folder = download(item_id, args.steamcmd, args.install, workshop_dir)
        found_mods, found_maps = discover(folder)
        chosen_mods = select(item.get("modIds"), found_mods, "modIds", item_id)
        chosen_maps = select(item.get("maps"), found_maps, "maps", item_id)
        if not chosen_mods:
            raise ValueError(f"No mod IDs selected for Workshop item {item_id}")
        if "Muldraugh, KY" in chosen_maps or set(chosen_maps) & set(maps):
            raise ValueError(f"{item_id}: duplicate or reserved map name in {chosen_maps}")
        ids.append(item_id)
        for mod_id in chosen_mods:
            if mod_id not in mod_ids:
                mod_ids.append(mod_id)
        maps.extend(chosen_maps)
        for map_name in chosen_maps:
            destination = args.install / "media" / "maps" / map_name
            for source in map_sources(folder, map_name):
                shutil.copytree(source, destination, dirs_exist_ok=True)
        print(f"Resolved {item.get('name', item_id)} ({item_id}): {chosen_mods}, maps: {chosen_maps}")
    # PZ expects the base map last. Do not rewrite any unrelated INI setting.
    update_ini(args.ini, {
        "WorkshopItems": ";".join(ids),
        "Mods": ";".join(mod_ids),
        "Map": ";".join(maps + ["Muldraugh, KY"]),
    })


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Mod resolution failed: {error}", file=sys.stderr)
        sys.exit(1)
