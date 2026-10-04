"""Build the FKZ Metamod plugins, deploy them into CS2 and start an insecure listen server on a map.

    py run-fkz.py                         # every default plugin, de_dust2
    py run-fkz.py --map kz_grotto         # another map
    py run-fkz.py -p menus -p admin       # only these plugins
    py run-fkz.py --no-build --no-launch  # just redeploy what's already built
    py run-fkz.py --reset-db              # wipe every plugin's SQLite database first (or name some: --reset-db admin kz)
    py run-fkz.py -- -dev +sv_cheats 1    # anything after -- goes to cs2.exe

CS2 has to be closed, since it locks the plugin DLLs.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import psutil

from common import backup_files, get_cs2_path, modify_gameinfo, restore_files

PLUGINS = {
    "admin": "mm-cs2admin",
    "menus": "mm-cs2menus",
    "rtv": "mm-cs2rockthevote",
    "whitelist": "mm-cs2whitelist",
    "fkz-api": "mm-fkz-api",
    "dressup": "cs2dressup",
}
# The whitelist kicks a host who isn't on it, so it's opt-in.
DEFAULT_PLUGINS = ["admin", "menus", "rtv", "fkz-api", "dressup"]
# Plugins with a "Database" block in cfg/<dir>/core.cfg: name -> (cfg dir, code default SQLite path).
DATABASE_BLOCKS = {
    "admin": ("cs2admin", "addons/cs2admin/data/cs2admin.db"),
    "menus": ("cs2menus", "addons/cs2menus/cs2menus.db"),
    "whitelist": ("cs2whitelist", "addons/cs2whitelist/whitelist.db"),
    "dressup": ("cs2dressup", "addons/cs2dressup/cs2dressup.db"),
}
# rtv keeps no database.
DATABASES = sorted(list(DATABASE_BLOCKS) + ["fkz-api", "kz"])
MENUS_ADDON = "cs2menus"
METAMOD_LINE = "csgo/addons/metamod"


def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


def cs2_running():
    return any(
        (p.info.get("name") or "").lower() == "cs2.exe"
        for p in psutil.process_iter(["name"])
    )


def copy_tree(src, dst, overwrite=True, suffix=None):
    """Copies every file under src into dst. Returns (copied, kept) counts."""
    copied = kept = 0
    for root, _, files in os.walk(src):
        target_dir = os.path.join(dst, os.path.relpath(root, src))
        for name in files:
            if suffix and not name.endswith(suffix):
                continue
            target = os.path.join(target_dir, name)
            if os.path.exists(target) and not overwrite:
                kept += 1
                continue
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(os.path.join(root, name), target)
            copied += 1
    return copied, kept


def remove_tree(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        fail(f"can't clear {path} ({e}), close CS2 and the Workshop Tools first")


def sync_mm_utils(repos, repo):
    # Plugins build against their own vendor/mm-utils.
    src = os.path.join(repos, "mm-utils")
    dst = os.path.join(repo, "vendor", "mm-utils")
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    print(f"  synced mm-utils into {dst}")


def build(repo):
    build_dir = os.path.join(repo, "build")
    # configure.py exits 0 even when it fails, so check for its output instead.
    if not os.path.isfile(os.path.join(build_dir, ".ambuild2", "vars")):
        os.makedirs(build_dir, exist_ok=True)
        subprocess.run(
            [sys.executable, os.path.join("..", "configure.py"), "--enable-optimize"],
            cwd=build_dir,
        )
        if not os.path.isfile(os.path.join(build_dir, ".ambuild2", "vars")):
            fail(f"configure.py failed in {build_dir}")
    if subprocess.run(["ambuild"], cwd=build_dir).returncode != 0:
        fail(f"build failed in {build_dir}")


def deploy(repo, csgo, reset_configs):
    package = os.path.join(repo, "build", "package")
    if not os.path.isdir(package):
        fail(f"no build output at {package}, build it first")
    copied, _ = copy_tree(os.path.join(package, "addons"), os.path.join(csgo, "addons"))
    print(f"  addons: {copied} files")
    cfg = os.path.join(package, "cfg")
    if os.path.isdir(cfg):
        # Keeps local test edits, only missing configs are added.
        copied, kept = copy_tree(
            cfg, os.path.join(csgo, "cfg"), overwrite=reset_configs
        )
        print(f"  cfg: {copied} copied, {kept} kept (--reset-configs overwrites)")


def compile_menus_layout(cs2, repo):
    """Compiles workshop/panorama with the Workshop Tools and drops the result loose into game/csgo."""
    bin_dir = os.path.join(cs2, "game", "bin", "win64")
    compiler = os.path.join(bin_dir, "resourcecompiler.exe")
    if not os.path.isfile(compiler):
        print(
            "  resourcecompiler.exe not found (CS2 Workshop Tools not installed), skipping the panorama layout"
        )
        return
    content = os.path.join(cs2, "content", "csgo_addons", MENUS_ADDON, "panorama")
    addon = os.path.join(cs2, "game", "csgo_addons", MENUS_ADDON)
    compiled = os.path.join(addon, "panorama")
    # Mirrors the repo, so the game side addon folder is exactly what the Workshop gets, no files left from renames.
    for stale in (content, compiled):
        remove_tree(stale)
    # A minimal copy rather than the repo's files, so what's compiled here is what a release ships.
    minified = subprocess.run(
        [sys.executable, os.path.join(repo, "workshop", "tools", "minify.py"), content],
        capture_output=True,
        text=True,
    )
    if minified.returncode != 0:
        print(minified.stdout + minified.stderr)
        fail("panorama minify failed")
    print(f"  panorama: {minified.stdout.strip()}")
    os.makedirs(addon, exist_ok=True)
    # The layouts derive from cs2kz's (AGPL-3.0), the license ships with the addon.
    shutil.copy2(
        os.path.join(repo, "workshop", "LICENSE"), os.path.join(addon, "LICENSE.txt")
    )
    result = subprocess.run(
        [compiler, "-nop4", "-f", "-r", "-i", os.path.join(content, "*")],
        cwd=bin_dir,
        capture_output=True,
        text=True,
        errors="replace",
    )
    summary = re.search(r"OK: (\d+) compiled, (\d+) failed", result.stdout)
    if result.returncode != 0 or not summary or summary.group(2) != "0":
        print(result.stdout[-3000:])
        fail("panorama compile failed")
    print(f"  panorama: {summary.group(1)} compiled")
    # Loose files load in an insecure listen server, like setup.py's cs2kz addon.
    # Only our own custom_game/cs2menus folders are cleared, other addons deploy loose files here too.
    loose = os.path.join(cs2, "game", "csgo", "panorama")
    for kind in ("layout", "styles", "images"):
        remove_tree(os.path.join(loose, kind, "custom_game", MENUS_ADDON))
    copied, _ = copy_tree(compiled, loose, suffix="_c")
    print(f"  panorama: {copied} files deployed loose")


def disable_menus_mount(csgo):
    # A mounted workshop VPK could shadow the loose layout that was just compiled.
    path = os.path.join(csgo, "cfg", "cs2menus", "core.cfg")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        text = f.read()
    patched = re.sub(r'("MountAddon"\s+)"1"', r'\1"0"', text)
    if patched != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(patched)
        print(
            "  set Panorama MountAddon to 0 in the local cs2menus core.cfg, so the local layout is used"
        )


def read_cfg(csgo, relative):
    path = os.path.join(csgo, "cfg", relative)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def kv_block(text, name):
    match = re.search(rf'"{re.escape(name)}"\s*{{([^{{}}]*)}}', text, re.I)
    return match.group(1) if match else None


def kv_value(text, key):
    match = re.search(rf'"{re.escape(key)}"\s+"([^"]*)"', text, re.I)
    return match.group(1) if match else None


def sqlite_path(csgo, name):
    """The SQLite file a plugin uses per the local config, relative to game/csgo. None when it's on MySQL or has no database."""
    if name in DATABASE_BLOCKS:
        cfg_dir, default = DATABASE_BLOCKS[name]
        block = (
            kv_block(read_cfg(csgo, os.path.join(cfg_dir, "core.cfg")), "Database")
            or ""
        )
        if (kv_value(block, "Type") or "sqlite").lower() != "sqlite":
            return None
        return kv_value(block, "Path") or kv_value(block, "db_path") or default
    if name == "fkz-api":
        # Plain cvar lines, and an empty driver disables the database.
        text = read_cfg(csgo, os.path.join("fkz-api", "core.cfg"))
        driver = re.search(r'^\s*db_driver\s+"([^"]*)"', text, re.M)
        database = re.search(r'^\s*db_database\s+"([^"]*)"', text, re.M)
        if not driver or driver.group(1).lower() != "sqlite":
            return None
        return (database and database.group(1)) or "addons/fkz-api/data/prefs.sqlite3"
    if name == "kz":
        # cs2kz takes a database name and stores it as addons/cs2kz/data/<name>.sqlite3.
        block = kv_block(read_cfg(csgo, "cs2kz-server-config.txt"), "db")
        if block is None or (kv_value(block, "driver") or "").lower() != "sqlite":
            return None
        return f"addons/cs2kz/data/{kv_value(block, 'database') or 'cs2kz'}.sqlite3"
    return None


def reset_databases(csgo, names):
    for name in names:
        relative = sqlite_path(csgo, name)
        if relative is None:
            print(f"  {name}: no SQLite database configured")
            continue
        path = os.path.join(csgo, relative)
        removed = []
        for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
            if os.path.isfile(candidate):
                os.remove(candidate)
                removed.append(os.path.basename(candidate))
        print(
            f"  {name}: {'deleted ' + ', '.join(removed) if removed else 'nothing at'} {os.path.dirname(path)}"
        )


def recover_gameinfo(gameinfo, backup):
    # A crashed run leaves Metamod in gameinfo, and backing that up would lose the original.
    with open(gameinfo, encoding="utf-8") as f:
        if METAMOD_LINE not in f.read():
            return
    if not os.path.isfile(backup):
        fail(
            f"{gameinfo} still loads Metamod from an earlier run and has no backup. Run verify.py first."
        )
    print("Restoring gameinfo left modified by an earlier run...")
    restore_files(backup, gameinfo)


def wait_for_dll(dll, timeout):
    print(f"Waiting for cs2.exe to load '{dll}'...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() != "cs2.exe":
                continue
            try:
                if any(dll in module.path.lower() for module in proc.memory_maps()):
                    return True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        time.sleep(0.5)
    return False


def launch(cs2, map_name, extra_args):
    csgo = os.path.join(cs2, "game", "csgo")
    gameinfo = os.path.join(csgo, "gameinfo.gi")
    recover_gameinfo(gameinfo, gameinfo + ".bak")
    gameinfo, backup = backup_files(cs2)
    try:
        modify_gameinfo(gameinfo)
        exe = os.path.join(cs2, "game", "bin", "win64", "cs2.exe")
        args = [
            exe,
            "-insecure",
            "+sv_cheats true",
            "+kz_ac_autokick 0",
            "+kz_profile_clantag_enabled false",
            "+map",
            map_name,
        ] + extra_args
        print(f"Launching {' '.join(args[1:])}")
        subprocess.Popen(args)
        # Keep the modified gameinfo until the game has actually read it.
        if not wait_for_dll("metamod.2.cs2.dll", 120):
            print("Metamod never loaded, check the game console.")
    finally:
        restore_files(backup, gameinfo)
        print("Restored the original gameinfo.")
        if os.path.exists("steam_appid.txt"):
            os.remove("steam_appid.txt")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-p",
        "--plugin",
        action="append",
        choices=sorted(PLUGINS),
        help=f"default: {' '.join(DEFAULT_PLUGINS)}",
    )
    parser.add_argument(
        "--repos",
        default=os.environ.get("FKZ_REPOS", os.path.join(here, "..", ".fkz")),
        help="folder holding the plugin repos (default: $FKZ_REPOS or ../.fkz)",
    )
    parser.add_argument("--map", default="de_nuke")
    parser.add_argument(
        "--no-build", action="store_true", help="deploy the existing build output"
    )
    parser.add_argument(
        "--no-launch", action="store_true", help="build and deploy, don't start CS2"
    )
    parser.add_argument(
        "--sync-mm-utils",
        action="store_true",
        help="copy the standalone mm-utils into each plugin's vendor/mm-utils first",
    )
    parser.add_argument(
        "--reset-configs",
        action="store_true",
        help="overwrite local cfg files with the repo defaults",
    )
    parser.add_argument(
        "--reset-db",
        nargs="*",
        choices=DATABASES,
        metavar="NAME",
        help=f"delete these plugins' SQLite databases before launching, all when none are named ({' '.join(DATABASES)})",
    )
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]

    cs2 = get_cs2_path()
    if cs2 is None:
        fail("CS2 not found")
    csgo = os.path.join(cs2, "game", "csgo")
    if not os.path.isdir(os.path.join(csgo, "addons", "metamod")):
        fail("Metamod is not installed, run setup.py first")
    if cs2_running():
        fail("close CS2 first, it locks the plugin DLLs")

    repos = os.path.abspath(args.repos)
    for name in args.plugin or DEFAULT_PLUGINS:
        repo = os.path.join(repos, PLUGINS[name])
        if not os.path.isdir(repo):
            fail(f"{repo} not found")
        print(f"== {name} ({repo})")
        if args.sync_mm_utils:
            sync_mm_utils(repos, repo)
        if not args.no_build:
            build(repo)
        deploy(repo, csgo, args.reset_configs)
        if name == "menus":
            compile_menus_layout(cs2, repo)
            disable_menus_mount(csgo)

    # After deploying, so a fresh cfg decides the path.
    if args.reset_db is not None:
        print("== databases")
        reset_databases(csgo, args.reset_db or DATABASES)

    if not args.no_launch:
        launch(cs2, args.map, extra)


if __name__ == "__main__":
    main()
