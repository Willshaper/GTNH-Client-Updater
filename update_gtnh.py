import os
import sys
import shutil
import zipfile
import glob
import re
import json
import time
import urllib.request

# Emoji/box-drawing output crashes on Windows when stdout is a pipe or log file
# (the console works, but redirected output falls back to cp1252). Force UTF-8 so
# output never raises, degrading gracefully where the target can't render a glyph.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Configuration ─────────────────────────────────────────────────────────────

# Root folder where all Prism Launcher instances live. Uses %APPDATA% on Windows;
# elsewhere it falls back to "" (so set FIXED_INSTANCE_DIR below, or edit this).
INSTANCES_ROOT = os.path.join(os.environ.get("APPDATA", ""), "PrismLauncher", "instances")

# Optional: set a full path to always use a specific instance and skip the
# selection menu entirely. Leave as an empty string "" to use the menu.
FIXED_INSTANCE_DIR = r""

# GTNH instances are detected by the mods they contain, not by folder name (a
# name like "GT Odyssee" should NOT match). An instance counts as GTNH if its
# mods/ folder holds a jar whose name contains one of these markers. These are
# mods that are exclusive to (or definitive of) GT: New Horizons — plain
# "gregtech" is deliberately NOT here, since other GregTech packs ship it too.
GTNH_MOD_MARKERS = [
    "newhorizonscoremod",  # the GTNH modpack coremod — present in every GTNH pack
    "gtnewhorizons",
    "dreamcraft",
    "bartworks",
    "tectech",
    "gtplusplus",
    "gt++",
    "detrav",              # Detrav Scanner Mod (GTNH)
]

DOWNLOAD_URL = "https://www.gtnewhorizons.com/downloads/"

# Page listing all GTNH releases (and the actual client-pack download links).
VERSION_HISTORY_URL = "https://www.gtnewhorizons.com/version-history"

# Daily / Experimental builds are produced by GitHub Actions in this repo, as
# full Prism client packs (the "mmcprism-java17-25" artifact). GitHub artifacts
# need auth to download from the API, so we fetch them through nightly.link, a
# public mirror that needs no token. Set GITHUB_TOKEN in the environment to use
# the official API instead (and to lift the 60-requests/hour anonymous limit).
GTNH_BUILD_REPO = "GTNewHorizons/DreamAssemblerXXL"
BUILD_WORKFLOWS = {
    "daily": "daily-modpack-build.yml",
    "experimental": "experimental-modpack-build.yml",
}
CLIENT_ARTIFACT_MARKER = "mmcprism-java17-25"

# Personal files kept across an update. Two kinds, handled the same way by the
# restore step (it copies back whatever it finds in the rollback snapshot):
#
#   * Items INSIDE a replaced folder (config/) are moved into the snapshot during
#     the wipe and actively restored on top of the new pack's config.
#   * Items OUTSIDE every replaced folder are never deleted or overwritten by the
#     updater, so they survive in place untouched. They're listed here too so the
#     pre-update summary is complete and nothing quietly depends on luck.
#
# Paths are relative to the .minecraft folder.
CLIENT_PRESERVE = [
    # ── inside config/ (replaced) → actively restored from the snapshot ──
    "config/journeymap",          # JourneyMap explored-map (fog of war) state
    "config/NEI",                 # NEI settings, hidden items, bookmarks
    "config/shaders.properties",  # active shader selection
    "config/vendingmachine",      # vending machine favourites
    # ── outside replaced folders → survive in place, never touched ──
    "journeymap",                 # JourneyMap waypoints and maps
    "visualprospecting",          # JourneyMap ore vein data
    "TCNodeTracker",              # JourneyMap Thaumcraft node data
    "saves",                      # singleplayer worlds and NEI data
    "schematics",                 # saved schematics
    "screenshots",                # screenshots
    "shaderpacks",                # shader pack files
    "resourcepacks",              # resource packs (new pack ones merged in too)
    "opencomputers",              # OpenComputers data
    "maps",                       # map data
    "options.txt",                # game options
    "optionsof.txt",              # OptiFine options
    "optionsnf.txt",              # NotFine options
    "servers.dat",                # multiplayer server list
    "localconfig.cfg",            # local config changes
    "BotaniaVars.dat",            # Lexica Botania bookmarks
]

# File holding your personal config patches / overrides (created on first run).
CONFIG_PATCHES_FILE = "config_patches.json"

# ─────────────────────────────────────────────────────────────────────────────
# These are set at runtime by select_instance() — do not edit them here.
INSTANCE_DIR  = ""
MINECRAFT_DIR = ""
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
ROLLBACK_DIR  = os.path.join(SCRIPT_DIR, "_rollback")

TOTAL_STEPS = 8


# ── Colour ─────────────────────────────────────────────────────────────────────
# ANSI colours, used only when writing to a real terminal (never when output is
# piped/redirected, so logs stay clean). On Windows we enable VT processing first.
_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "grey": "\033[90m",
    "bred": "\033[91m", "bgreen": "\033[92m", "byellow": "\033[93m", "bcyan": "\033[96m",
}
USE_COLOR = False


def enable_color():
    """Turn on ANSI colour if stdout is an interactive terminal."""
    global USE_COLOR
    if not sys.stdout.isatty():
        USE_COLOR = False
        return
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
        except Exception:
            pass
    USE_COLOR = True


def color(text, *names):
    """Wrap text in the given ANSI styles (a no-op when colour is disabled)."""
    if not USE_COLOR or not names:
        return text
    prefix = "".join(_ANSI.get(n, "") for n in names)
    return f"{prefix}{text}{_ANSI['reset']}"


def separator(char="─", width=62):
    # Colour-code dividers by kind: ═ headers (cyan), ! warnings (yellow), else grey.
    tone = {"═": "cyan", "!": "yellow"}.get(char, "grey")
    print(color(char * width, tone))


def clear_screen():
    """Clear the console so the next screen starts at the top. No-op when output
    is redirected (piped/logged), to avoid dumping control codes into a file."""
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


# "GTNH" in the ANSI Shadow figlet font.
GTNH_BANNER = [
    " ██████╗ ████████╗███╗   ██╗██╗  ██╗",
    "██╔════╝ ╚══██╔══╝████╗  ██║██║  ██║",
    "██║  ███╗   ██║   ██╔██╗ ██║███████║",
    "██║   ██║   ██║   ██║╚██╗██║██╔══██║",
    "╚██████╔╝   ██║   ██║ ╚████║██║  ██║",
    " ╚═════╝    ╚═╝   ╚═╝  ╚═══╝╚═╝  ╚═╝",
]


def print_banner():
    print()
    for line in GTNH_BANNER:
        print("  " + color(line, "bgreen"))
    print()
    print(color("              By Willshaper", "grey"))


def screen(title):
    """Start a fresh titled screen: clear, then a cyan ═ rule / bold title / ═ rule
    header — the same layout as the main menu and the config-patch editor."""
    clear_screen()
    print()
    separator("═")
    print("  " + color(title, "bold"))
    separator("═")
    print()


def option(key, label):
    """Render one '[KEY] Label' button row (cyan key), the style used everywhere."""
    print(f"    {color(f'[{key}]', 'bcyan')} {label}")


def choose(default=None):
    """The single choice prompt used on every menu/selection screen. Returns the
    reply stripped and lowercased. `default` (shown as '[Enter = X]') is what a
    blank Enter maps to."""
    hint = f" [Enter = {default}]" if default is not None else ""
    return input(color(f"  Choose{hint}: ", "bold")).strip().lower()


def pause_exit(code=0):
    """Wait for Enter, then exit the whole program with the given code.
    Use code=1 for genuine errors so wrapping scripts can tell success from failure."""
    input("\nPress Enter to exit...")
    sys.exit(code)


class _Cancelled(Exception):
    """The user backed out of the current update; unwind to the main menu."""


def _return_to_menu():
    """Pause so the message above can be read, then unwind to the main menu."""
    input("\n  Press Enter to return to the menu...")
    raise _Cancelled()


# ── Instance selection ────────────────────────────────────────────────────────

def is_gtnh_instance(instance_dir):
    """
    Decide whether an instance is GT: New Horizons by inspecting its mods, not its
    name. Returns True if mods/ (or mods/1.7.10/) contains a jar matching one of
    the GTNH-specific markers. This keeps look-alikes such as "GT Odyssee" out.
    """
    mods_base = os.path.join(instance_dir, ".minecraft", "mods")
    mods_dirs = [mods_base, os.path.join(mods_base, "1.7.10")]

    for mods_dir in mods_dirs:
        if not os.path.isdir(mods_dir):
            continue
        try:
            names = os.listdir(mods_dir)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if not (low.endswith(".jar") or low.endswith(".jar.disabled")):
                continue
            if any(marker in low for marker in GTNH_MOD_MARKERS):
                return True
    return False


def select_instance():
    """
    Resolve which instance to update.
    - If FIXED_INSTANCE_DIR is set, validate it and use it directly.
    - Otherwise scan INSTANCES_ROOT, detect which instances are actually GTNH
      (by their mods), and let the user pick from a numbered list.
    Sets the global INSTANCE_DIR and MINECRAFT_DIR.
    """
    global INSTANCE_DIR, MINECRAFT_DIR

    # ── Fixed path override ───────────────────────────────────────────────────
    if FIXED_INSTANCE_DIR.strip():
        path = FIXED_INSTANCE_DIR.strip()
        if not os.path.isdir(path):
            separator("═")
            print("  ❌  The path set in FIXED_INSTANCE_DIR does not exist!")
            separator("═")
            print()
            print(f"  Path : {path}")
            print()
            print("  Please check the path in the script and run it again.")
            print()
            pause_exit(1)
        INSTANCE_DIR  = path
        MINECRAFT_DIR = os.path.join(INSTANCE_DIR, ".minecraft")
        print(f"  Using fixed instance : {os.path.basename(INSTANCE_DIR)}")
        print()
        return

    # ── Auto-scan instances folder ────────────────────────────────────────────
    if not os.path.isdir(INSTANCES_ROOT):
        separator("═")
        print("  ❌  Instances folder not found!")
        separator("═")
        print()
        print(f"  Expected : {INSTANCES_ROOT}")
        print()
        print("  Please update INSTANCES_ROOT in the script and run it again.")
        print()
        pause_exit(1)

    all_dirs = sorted([
        d for d in os.listdir(INSTANCES_ROOT)
        if os.path.isdir(os.path.join(INSTANCES_ROOT, d))
    ])

    # Detect GTNH by mod content (reliable — ignores misleading folder names).
    matching = [d for d in all_dirs if is_gtnh_instance(os.path.join(INSTANCES_ROOT, d))]
    detected = True

    if not matching:
        # Nothing recognised as GTNH. Rather than dead-end, show every instance
        # so the user can still pick (e.g. a brand-new instance with no mods yet).
        detected = False
        matching = all_dirs

    if not matching:
        separator("═")
        print("  ❌  No instances found at all!")
        separator("═")
        print()
        print(f"  Scanned : {INSTANCES_ROOT}")
        print()
        print("  Create your GT: New Horizons instance in the launcher first, or set")
        print("  FIXED_INSTANCE_DIR in the script to the full path of your instance.")
        print()
        pause_exit(1)

    screen("Select an instance")
    if not detected:
        print(color("  ⚠️   Could not auto-detect a GTNH instance — showing ALL instances.", "byellow"))
        print(color("       Pick your real GTNH one, not another GregTech pack.", "grey"))
        print()
    for i, name in enumerate(matching, 1):
        option(i, name)
    option("Q", "Go back")
    print()

    while True:
        raw = choose()
        if raw == "q":
            raise _Cancelled()
        if raw.isdigit() and 1 <= int(raw) <= len(matching):
            choice = int(raw)
            break
        print(color(f"  Not a valid choice — enter 1-{len(matching)} or Q.", "yellow"))

    chosen = matching[choice - 1]
    INSTANCE_DIR  = os.path.join(INSTANCES_ROOT, chosen)
    MINECRAFT_DIR = os.path.join(INSTANCE_DIR, ".minecraft")
    print()
    print(f"  Selected : {color(chosen, 'bgreen')}")
    print()


# ── Full instance backup ───────────────────────────────────────────────────────

BACKUP_SUFFIX = " - Backup"
BACKUP_GROUP = "Backup"


def _copy_tree_with_progress(src, dst):
    """Recursively copy src → dst with a live progress bar. Per-file errors (e.g.
    a locked file) are skipped and counted, not fatal. Returns the skipped count."""
    files = []          # (src_path, rel_path, size)
    dirs = []
    total = 0
    for root, _dnames, fnames in os.walk(src):
        rel = os.path.relpath(root, src)
        dirs.append(rel)
        for fn in fnames:
            fp = os.path.join(root, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            files.append((fp, os.path.join(rel, fn) if rel != "." else fn, sz))
            total += sz

    # Re-create the directory skeleton first (preserves empty folders).
    for rel in dirs:
        os.makedirs(dst if rel == "." else os.path.join(dst, rel), exist_ok=True)

    done = 0
    skipped = 0
    start = time.time()
    for i, (fp, rel, sz) in enumerate(files, 1):
        try:
            shutil.copy2(fp, os.path.join(dst, rel))
        except OSError:
            skipped += 1
        done += sz
        if total and (i % 25 == 0 or i == len(files)):
            pct = int(done * 100 / total)
            filled = pct // 4
            elapsed = time.time() - start
            speed = (done / elapsed / 1048576) if elapsed > 0 else 0
            bar = color("█" * filled, "bgreen") + color("─" * (25 - filled), "grey")
            print(f"\r  [{bar}] {color(f'{pct:3d}%', 'bcyan')}  "
                  f"{done/1048576:7.1f}/{total/1048576:.1f} MB  {speed:4.1f} MB/s",
                  end="", flush=True)
    print()
    return skipped


def _set_instance_name(cfg_path, new_name):
    """Rewrite the name= line in a Prism instance.cfg, preserving line endings."""
    if not os.path.isfile(cfg_path):
        return
    with open(cfg_path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    for i, line in enumerate(lines):
        if line.startswith("name="):
            lines[i] = "name=" + new_name
            break
    with open(cfg_path, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines))


def _add_to_backup_group(instances_dir, backup_name):
    """Add backup_name to the 'Backup' group in instgroups.json (create as needed)."""
    groups_path = os.path.join(instances_dir, "instgroups.json")
    data = {"formatVersion": "1", "groups": {}}
    if os.path.isfile(groups_path):
        try:
            with open(groups_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(color(f"  ⚠️   Could not read instgroups.json ({e}) — leaving groups alone.", "yellow"))
            return
    groups = data.setdefault("groups", {})
    group = groups.setdefault(BACKUP_GROUP, {"hidden": False, "instances": []})
    instances = group.setdefault("instances", [])
    if backup_name not in instances:
        instances.append(backup_name)
    with open(groups_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")


def make_full_backup():
    """Copy the whole instance folder to '<name> - Backup' and register it in Prism."""
    src = os.path.normpath(INSTANCE_DIR)
    instances_dir = os.path.dirname(src)
    backup_name = os.path.basename(src) + BACKUP_SUFFIX
    dst = os.path.join(instances_dir, backup_name)

    if os.path.exists(dst):
        print(color(f"  A backup already exists: {backup_name}", "yellow"))
        ans = input("  Overwrite it with a fresh backup? [y/N]: ").strip().lower()
        if ans != "y":
            print("  Keeping the existing backup.")
            _add_to_backup_group(instances_dir, backup_name)  # ensure it's grouped
            print()
            return
        try:
            shutil.rmtree(dst)
        except OSError as e:
            print(color(f"  ❌  Could not remove the old backup: {e}", "bred"))
            print()
            return

    print(f"  Backing up the full instance → {color(backup_name, 'bcyan')}")
    print(color("  (entire folder, including saves — may take a while / a lot of disk)", "grey"))
    try:
        skipped = _copy_tree_with_progress(src, dst)
    except Exception as e:
        print(color(f"  ❌  Backup failed: {e}", "bred"))
        if os.path.isdir(dst):
            try:
                shutil.rmtree(dst)
            except OSError:
                pass
        ans = input("  Continue the update WITHOUT a backup? [y/N]: ").strip().lower()
        if ans != "y":
            raise _Cancelled()
        print()
        return

    # Rename the copy so Prism shows it as the backup, and put it in the group.
    _set_instance_name(os.path.join(dst, "instance.cfg"), backup_name)
    _add_to_backup_group(instances_dir, backup_name)

    if skipped:
        print(color(f"  ⚠️   {skipped} file(s) could not be copied (in use?) and were skipped.", "yellow"))
    print(color(f"  ✅  Backup created and added to the '{BACKUP_GROUP}' group.", "green"))
    print()


def offer_full_backup():
    """Ask whether to make a full backup of the selected instance before updating."""
    screen("Full backup")
    print("  Copy the entire instance — saves, configs, everything — to a new")
    print(f"  '{os.path.basename(os.path.normpath(INSTANCE_DIR))}{BACKUP_SUFFIX}'")
    print(f"  instance in Prism, placed in the '{BACKUP_GROUP}' group.")
    print(color("  (Close Prism first so the group change isn't overwritten.)", "grey"))
    print()
    option("Y", "Yes, make a full backup first")
    option("N", "No, skip it")
    print()
    ans = choose(default="N")
    print()
    if ans == "y":
        make_full_backup()
        input(color("  Press Enter to continue...", "grey"))


# ── Step 0: Validate zip before touching anything ─────────────────────────────

def find_and_validate_zip(offer_download=False):
    zips = glob.glob(os.path.join(SCRIPT_DIR, "*.zip"))

    if not zips:
        screen("No pack zip found")
        print("  There's no .zip in the script folder:")
        print(color(f"    {SCRIPT_DIR}", "grey"))
        print()
        if offer_download:
            # The user chose "use a local zip" but there isn't one — offer the
            # download path instead of dead-ending back at the menu.
            option("D", "Download a build instead")
            option("Q", "Go back")
            print()
            if choose() == "d":
                print()
                return _choose_and_download()
            raise _Cancelled()
        print("  Download the pack from:")
        print(color(f"    {DOWNLOAD_URL}", "grey"))
        print("  then move the .zip into the folder above and run again.")
        print()
        _return_to_menu()

    if len(zips) > 1:
        screen("Select a pack zip")
        print("  Multiple .zip files are in the script folder:")
        print()
        for i, z in enumerate(zips, 1):
            option(i, os.path.basename(z))
        option("Q", "Go back")
        print()
        while True:
            raw = choose()
            if raw == "q":
                raise _Cancelled()
            if raw.isdigit() and 1 <= int(raw) <= len(zips):
                choice = int(raw)
                break
            print(color(f"  Not a valid choice — enter 1-{len(zips)} or Q.", "yellow"))
        zip_path = zips[choice - 1]
        print(f"  Using: {os.path.basename(zip_path)}")
        print()
    else:
        zip_path = zips[0]
    zip_name = os.path.basename(zip_path)

    if "java_8" in zip_name.lower():
        separator("!")
        print("  ⚠️   Java 8 version detected!")
        separator("!")
        print()
        print(f"  Zip file : {zip_name}")
        print()
        print("  This version of the pack requires Java 8.")
        print("  The recommended version is Java 17 or newer.")
        print()
        print("  Are you sure this is the correct version for your setup?")
        answer = input("  Type YES to continue with Java 8, or anything else to cancel: ").strip()
        if answer != "YES":
            raise _Cancelled()
        print()

    return zip_path


# ── Auto-download from gtnewhorizons.com ───────────────────────────────────────

def _is_valid_zip(path):
    """Cheap structural check — catches truncated/corrupt downloads."""
    try:
        with zipfile.ZipFile(path) as zf:
            return len(zf.namelist()) > 0
    except Exception:
        return False


def fetch_available_releases():
    """
    Scrape the version-history page for downloadable Java 17-25 client packs.
    Only releases that actually have a download link on the page are returned
    (older versions are no longer hosted), newest first.
    Returns a list of dicts: {version, type, url, name}. Empty list on failure.
    """
    try:
        req = urllib.request.Request(VERSION_HISTORY_URL, headers={"User-Agent": "GTNH-Updater-Script"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  (could not reach {VERSION_HISTORY_URL}: {e})")
        return []

    # Client packs only (Java 17-25). Betas live under a /betas/ subpath; we read
    # the real URLs straight from the page so that path is always correct.
    links = re.findall(
        r"https://downloads\.gtnewhorizons\.com/Multi_mc_downloads/[^\"'\s]+_Java_17-25\.zip",
        html,
    )

    seen = set()
    releases = []
    for url in links:
        name = url.rsplit("/", 1)[-1]
        mo = re.match(r"GT_New_Horizons_(.+)_Java_17-25\.zip$", name)
        if not mo:
            continue
        version = mo.group(1)
        if version in seen:
            continue
        seen.add(version)
        is_beta = ("/betas/" in url) or bool(re.search(r"(beta|rc)", version, re.IGNORECASE))
        releases.append({
            "version": version,
            "type": "Beta" if is_beta else "Stable",
            "url": url,
            "name": name,
        })

    return releases


def _github_json(path):
    """GET https://api.github.com/repos/<GTNH_BUILD_REPO>/<path> and parse JSON.
    Uses GITHUB_TOKEN from the environment if present (higher rate limit)."""
    url = f"https://api.github.com/repos/{GTNH_BUILD_REPO}/{path}"
    headers = {"User-Agent": "GTNH-Updater-Script", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_builds(channel, limit=10):
    """
    Return up to `limit` recent successful Daily/Experimental builds, newest first.
    Each is a dict {version, name, url, token} for its client (mmcprism, Java
    17-25) pack. Empty list on failure.

    Each pack ships as a GitHub Actions artifact. We download it via nightly.link
    (no auth) unless GITHUB_TOKEN is set, in which case the official API is used.
    """
    workflow = BUILD_WORKFLOWS[channel]
    # Fetch more runs than needed — some may lack a valid (non-expired) artifact.
    try:
        runs = _github_json(f"actions/workflows/{workflow}/runs?status=success&per_page={max(limit * 2, 20)}")
    except Exception as e:
        print(f"  (could not query GitHub Actions: {e})")
        return []

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    builds = []
    for run in runs.get("workflow_runs", []):
        if len(builds) >= limit:
            break
        try:
            arts = _github_json(f"actions/runs/{run['id']}/artifacts")
        except Exception:
            continue
        for a in arts.get("artifacts", []):
            name = a.get("name", "")
            if CLIENT_ARTIFACT_MARKER not in name or a.get("expired", True):
                continue
            aid = a["id"]
            # The artifact name is the pack zip name; GitHub wraps it in an outer
            # zip, which the extractor's double-zip detection unpacks.
            zip_name = name if name.lower().endswith(".zip") else name + ".zip"
            version = re.sub(r"^GTNH-", "", zip_name)
            version = re.sub(r"-" + re.escape(CLIENT_ARTIFACT_MARKER) + r"\.zip$", "", version)
            if token:
                url = f"https://api.github.com/repos/{GTNH_BUILD_REPO}/actions/artifacts/{aid}/zip"
            else:
                url = f"https://nightly.link/{GTNH_BUILD_REPO}/actions/artifacts/{aid}.zip"
            builds.append({"version": version, "name": zip_name, "url": url, "token": bool(token)})
            break  # one client artifact per run
    return builds


def select_build(builds, channel):
    """Show a numbered picker (newest first) and return the chosen build dict."""
    label = channel.capitalize()
    screen(f"{label} builds")
    print("  Newest first:")
    print()
    for i, b in enumerate(builds, 1):
        marker = color("   ← latest", "bgreen") if i == 1 else ""
        option(i, f"{b['version']}{marker}")
    option("Q", "Go back")
    print()
    while True:
        raw = choose(default=1)
        if raw == "":
            return builds[0]
        if raw == "q":
            raise _Cancelled()
        if raw.isdigit() and 1 <= int(raw) <= len(builds):
            return builds[int(raw) - 1]
        print(color(f"  Not a valid choice — enter 1-{len(builds)} or Q.", "yellow"))


def select_release(releases):
    """Show a numbered picker (newest first) and return the chosen release dict."""
    screen("Releases")
    print("  Newest first:")
    print()

    latest_stable = next((i for i, r in enumerate(releases) if r["type"] == "Stable"), None)
    for i, r in enumerate(releases, 1):
        marker = color("   ← latest stable", "bgreen") if (latest_stable is not None and i - 1 == latest_stable) else ""
        tone = "green" if r["type"] == "Stable" else "yellow"
        tag = color(f"{r['type']:<6}", tone)
        option(i, f"{r['version']:<18} {tag}{marker}")
    option("Q", "Go back")
    print()

    default = (latest_stable + 1) if latest_stable is not None else 1
    while True:
        raw = choose(default=default)
        if raw == "":
            return releases[default - 1]
        if raw == "q":
            raise _Cancelled()
        if raw.isdigit() and 1 <= int(raw) <= len(releases):
            return releases[int(raw) - 1]
        print(color(f"  Not a valid choice — enter 1-{len(releases)} or Q.", "yellow"))


def _download_with_progress(url, dest, extra_headers=None):
    """Stream a download to dest with a live progress bar. Writes to .part first."""
    part = dest + ".part"
    headers = {"User-Agent": "GTNH-Updater-Script"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        chunk = 256 * 1024
        start = time.time()
        with open(part, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                elapsed = time.time() - start
                speed = (done / elapsed / 1048576) if elapsed > 0 else 0
                if total:
                    pct = int(done * 100 / total)
                    filled = pct // 4
                    bar = color("█" * filled, "bgreen") + color("─" * (25 - filled), "grey")
                    print(f"\r  [{bar}] {color(f'{pct:3d}%', 'bcyan')}  "
                          f"{done/1048576:6.1f}/{total/1048576:.1f} MB  {speed:4.1f} MB/s",
                          end="", flush=True)
                else:
                    print(f"\r  {done/1048576:6.1f} MB  {speed:4.1f} MB/s", end="", flush=True)
    print()
    os.replace(part, dest)


def _download_to_folder(url, name, label, extra_headers=None):
    """
    Download url → SCRIPT_DIR/name with progress, reusing an existing valid copy.
    Returns the validated path, or falls back to find_and_validate_zip() on a
    network failure. Returns to the menu if the finished download isn't a zip.
    """
    dest = os.path.join(SCRIPT_DIR, name)

    # Don't re-pull a complete copy that's already sitting in the folder.
    if os.path.isfile(dest) and _is_valid_zip(dest):
        print(color(f"  ℹ️   {name} is already here — using it (skipping download).", "cyan"))
        print()
        return dest

    print()
    print(f"  Downloading {color(label, 'bcyan')}")
    print(color(f"  {url}", "grey"))
    try:
        _download_with_progress(url, dest, extra_headers=extra_headers)
    except Exception as e:
        for leftover in (dest + ".part", dest):
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        print(color(f"\n  ❌  Download failed: {e}", "bred"))
        print("      Falling back to using a local .zip.")
        print()
        return find_and_validate_zip()

    if not _is_valid_zip(dest):
        os.remove(dest)
        separator("═")
        print(color("  ❌  The downloaded file is not a valid zip (corrupted download).", "bred"))
        separator("═")
        print()
        print("  This is usually a temporary network issue — try again.")
        _return_to_menu()

    print(color(f"  ✅  Downloaded: {name}", "green"))
    print()
    return dest


def _download_stable():
    """Pick a Stable/Beta/RC release from the version-history page and download it."""
    print("  Fetching available releases...")
    releases = fetch_available_releases()
    if not releases:
        print("  ⚠️   Could not fetch the release list (no internet, or the site changed).")
        print("       Falling back to using a local .zip.")
        print()
        return find_and_validate_zip()

    chosen = select_release(releases)
    return _download_to_folder(
        chosen["url"], chosen["name"],
        f"GT: New Horizons {chosen['version']} [{chosen['type']}]",
    )


def _download_channel_build(channel):
    """Let the user pick from recent Daily/Experimental builds and download it."""
    label = channel.capitalize()
    print(f"  Finding recent {label} builds on GitHub...")
    builds = fetch_builds(channel, limit=10)
    if not builds:
        separator("!")
        print(f"  ⚠️   Could not find any downloadable {label} builds.")
        separator("!")
        print()
        print("  GitHub Actions / nightly.link may be unavailable or rate-limited")
        print("  (anonymous GitHub API allows 60 requests/hour — set GITHUB_TOKEN to")
        print("  raise it). Falling back to using a local .zip.")
        print()
        return find_and_validate_zip()

    chosen = select_build(builds, channel)
    if not chosen["token"]:
        print("  Source : nightly.link (public artifact mirror)")
    return _download_to_folder(
        chosen["url"], chosen["name"], f"GT: New Horizons {chosen['version']}",
        extra_headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN'].strip()}"}
        if chosen["token"] else None,
    )


def _choose_and_download():
    """Channel picker → download the chosen Stable / Daily / Experimental build."""
    screen("Download channel")
    option("1", color("Stable", "green") + "        official release (recommended)")
    option("2", color("Daily", "yellow") + "         latest dev build, rebuilt daily")
    option("3", color("Experimental", "bred") + "  bleeding edge, may be unstable")
    option("Q", "Go back")
    print()
    channel = choose(default=1)
    print()

    if channel == "q":
        raise _Cancelled()
    if channel == "2":
        return _download_channel_build("daily")
    if channel == "3":
        return _download_channel_build("experimental")
    return _download_stable()


def obtain_zip():
    """
    Resolve the pack zip to use. Offers downloading a build (Stable / Daily /
    Experimental) first, falling back to a local .zip on any failure.
    Returns the path to a validated zip.
    """
    screen("Get the pack")
    print("  How do you want to provide the GT: New Horizons pack?")
    print()
    option("1", "Download a build")
    option("2", "Use a .zip already in this folder")
    option("Q", "Go back")
    print()
    choice = choose(default=1)
    print()

    if choice == "q":
        raise _Cancelled()
    if choice == "2":
        return find_and_validate_zip(offer_download=True)
    return _choose_and_download()


# ── Config patches: load / template ────────────────────────────────────────────

def _strip_json_comments(text):
    """
    Make the patches file forgiving for hand editing:
      - drop whole lines whose first non-space char is // or #
      - remove trailing commas before } or ]
    Values are never on their own line in this schema, so full-line comment
    stripping is safe.
    """
    cleaned_lines = []
    for line in text.splitlines():
        if re.match(r'^\s*(//|#)', line):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
    return cleaned


# ── Built-in presets ───────────────────────────────────────────────────────────
# Toggleable bundles of config patches. Enabled presets are applied together with
# the user's own key_patches. Paths are relative to .minecraft. A preset is either
# fixed ("patches": [...]) or adjustable ("settings": [...] writing to one "file").
PRESETS = [
    {
        "id": "disable_gt_explosions",
        "name": "Disable GT Explosions",
        "note": "Disables GregTech machine explosions.",
        "patches": [
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineExplosions", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineFireExplosions", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineFlammable", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineNonWrenchExplosions", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineRainExplosions", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineThunderExplosions", "value": False},
            {"file": "config/GregTech/GregTech.cfg", "key": "B:machineWireFire", "value": False},
        ],
    },
    {
        "id": "remove_pollution",
        "name": "Remove Pollution",
        "note": "Disables the pollution mechanic.",
        "patches": [
            {"file": "config/GregTech/Pollution.cfg", "key": 'B:"Activate Pollution"', "value": False},
        ],
    },
    {
        "id": "restore_mobspawner_hardness",
        "name": "Restore Mobspawner Hardness",
        "note": "Restore default spawner breaking difficulty.",
        "patches": [
            {"file": "config/GregTech/GregTech.cfg", "key": "B:harderMobSpawner", "value": False},
        ],
    },
    {
        "id": "disable_blood_moon",
        "name": "Disable Blood Moon",
        "note": "Disables the Random Things Blood Moon.",
        "patches": [
            {"file": "config/RandomThings.cfg", "key": "D:BloodMoonChance", "value": "0.00"},
        ],
    },
    {
        "id": "serverutilities_settings",
        "name": "Adjust ServerUtilities Settings",
        "note": "Set Claim / Chunkloader / Home limits. Also turns ServerUtilities ranks\n"
                "  ON, otherwise these limits are ignored in singleplayer.",
        # Singleplayer reads ranks.txt only when ranks are enabled; otherwise it
        # uses hardcoded OP defaults. So enable ranks (and keep your normal chat name).
        "patches": [
            {"file": "serverutilities/serverutilities.cfg", "key": "B:enabled", "value": True, "section": "ranks",
             "note": "Enable ranks so the limits below apply in singleplayer"},
            {"file": "serverutilities/serverutilities.cfg", "key": "B:override_chat", "value": False, "section": "ranks",
             "note": "Keep your normal chat name (no rank prefix)"},
            {"file": "serverutilities/serverutilities.cfg", "key": "B:chunk_claiming", "value": True, "section": "world",
             "note": "Enable chunk claiming (needed for claims/chunkloading)"},
        ],
        "file": "serverutilities/server/ranks.txt",
        "sep": ": ",
        "settings": [
            {"id": "claim_amount",       "label": "Claim Amount",       "key": "serverutilities.claims.max_chunks",      "min": 0, "max": 10000, "default": 1000},
            {"id": "chunkloader_amount", "label": "Chunkloader Amount", "key": "serverutilities.chunkloader.max_chunks", "min": 0, "max": 10000, "default": 1000},
            {"id": "home_amount",        "label": "Home Amount",        "key": "serverutilities.homes.max",              "min": 0, "max": 10000, "default": 1},
        ],
    },
    {
        "id": "su_commands",
        "name": "ServerUtilities Commands",
        "note": "Turn individual ServerUtilities commands on/off. GTNH disables many\n"
                "  by default; flip the ones you want, then enable the preset.",
        "file": "serverutilities/serverutilities.cfg",
        "section": "commands",
        "toggles": [
            {"id": "home",  "label": "/home",  "key": "B:home",  "default": False},
            {"id": "back",  "label": "/back",  "key": "B:back",  "default": False},
            {"id": "spawn", "label": "/spawn", "key": "B:spawn", "default": False},
            {"id": "tpa",   "label": "/tpa",   "key": "B:tpa",   "default": False},
            {"id": "warp",  "label": "/warp",  "key": "B:warp",  "default": False},
            {"id": "rtp",   "label": "/rtp",   "key": "B:rtp",   "default": False},
            {"id": "fly",   "label": "/fly",   "key": "B:fly",   "default": False},
            {"id": "god",   "label": "/god",   "key": "B:god",   "default": False},
            {"id": "heal",  "label": "/heal",  "key": "B:heal",  "default": False},
            {"id": "nick",  "label": "/nick",  "key": "B:nick",  "default": False},
            {"id": "mute",  "label": "/mute",  "key": "B:mute",  "default": False},
            {"id": "kickme", "label": "/kickme", "key": "B:kickme", "default": False},
            {"id": "rec",   "label": "/rec",   "key": "B:rec",   "default": False},
        ],
    },
]


def _default_presets_state():
    """The saved on/off (and values/toggles) state for every preset, all disabled."""
    state = {}
    for p in PRESETS:
        entry = {"enabled": False}
        if "settings" in p:
            entry["values"] = {s["id"]: s["default"] for s in p["settings"]}
        if "toggles" in p:
            entry["toggles"] = {t["id"]: t["default"] for t in p["toggles"]}
        state[p["id"]] = entry
    return state


def preset_patches(presets_state):
    """Return the config patches contributed by all ENABLED presets. A preset may
    carry fixed 'patches', adjustable numeric 'settings', and/or boolean 'toggles'."""
    out = []
    for p in PRESETS:
        st = presets_state.get(p["id"], {})
        if not st.get("enabled"):
            continue
        for patch in p.get("patches", []):
            out.append({**patch, "note": patch.get("note", p["name"])})
        if "settings" in p:
            values = st.get("values", {})
            for s in p["settings"]:
                out.append({
                    "file": p["file"], "key": s["key"],
                    "value": values.get(s["id"], s["default"]),
                    "sep": p.get("sep", "="), "all": True,
                    "note": f"{p['name']}: {s['label']}",
                })
        if "toggles" in p:
            toggles = st.get("toggles", {})
            for t in p["toggles"]:
                out.append({
                    "file": p["file"], "key": t["key"],
                    "value": bool(toggles.get(t["id"], t["default"])),
                    "section": p.get("section", ""),
                    "note": f"{p['name']}: {t['label']}",
                })
    return out


def _patches_doc(key_patches, presets):
    """Build the full config_patches.json document (help text + examples + data)."""
    return {
        "_README": [
            "GTNH Updater - Config Patches",
            "",
            "This file changes individual settings INSIDE the new pack's config",
            "files. key_patches are applied after each update (and from the menu's",
            "'Apply patches & files' option); everything else stays at the default.",
            "",
            "All paths are relative to your instance's .minecraft folder.",
            "Lines starting with // or # are ignored, so you can leave notes.",
            "",
            "To keep your WHOLE version of a file instead of patching single keys,",
            "drop it in the 'Additional Files' folder.",
            "",
            "key_patches fields: file, key (incl. Forge prefix B:/I:/D:/S:), value,",
            "  section (optional), note (optional). Copy an _examples entry to start.",
            "",
            "presets: built-in toggleable bundles. Edit them in the app via",
            "  'Edit config patches' > 'Manage presets' (recommended over hand-editing).",
        ],
        "_examples": {
            "key_patches": [
                {"file": "config/GregTech.cfg", "key": "B:EnablePollution",
                 "value": "false", "section": "general", "note": "Turn off pollution"}
            ]
        },
        "key_patches": key_patches,
        "presets": presets,
    }


def _write_config(path, key_patches, presets):
    """Write config_patches.json (regenerates help text + examples each time)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_patches_doc(key_patches, presets), f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_config(path):
    """
    Read (key_patches, presets_state) from the patches file. Missing file →
    ([], defaults). Unknown presets / missing values fall back to defaults.
    Raises on a malformed file (caller decides what to do).
    """
    presets = _default_presets_state()
    if not os.path.isfile(path):
        return [], presets
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(_strip_json_comments(raw))
    key_patches = [p for p in (data.get("key_patches") or [])
                   if isinstance(p, dict) and p.get("file") and p.get("key") is not None]
    for pid, st in (data.get("presets") or {}).items():
        if pid not in presets or not isinstance(st, dict):
            continue
        if "enabled" in st:
            presets[pid]["enabled"] = bool(st["enabled"])
        if isinstance(st.get("values"), dict):
            presets[pid].setdefault("values", {}).update(
                {k: v for k, v in st["values"].items() if isinstance(v, int)})
        if isinstance(st.get("toggles"), dict):
            presets[pid].setdefault("toggles", {}).update(
                {k: bool(v) for k, v in st["toggles"].items() if isinstance(v, bool)})
    return key_patches, presets


def load_config_patches():
    """
    Load key_patches from CONFIG_PATCHES_FILE. Creates a commented template on
    first run. Returns the list of key patches.
    A malformed file warns loudly but does not abort the update.
    """
    path = os.path.join(SCRIPT_DIR, CONFIG_PATCHES_FILE)

    if not os.path.isfile(path):
        _write_config(path, [], _default_presets_state())
        separator()
        print("  📝  Created config patches file (first run):")
        print(f"      {path}")
        separator()
        print()
        print("  Carry personal config tweaks across updates. It's empty for now;")
        print("  edit it via the menu's 'Edit config patches' (add key patches or")
        print("  enable presets), or hand-edit the file. Then re-run.")
        print()
        return []

    try:
        key_patches, presets = _read_config(path)
        return key_patches + preset_patches(presets)
    except Exception as e:
        separator("!")
        print("  ⚠️   Could not read config_patches.json — skipping patches!")
        separator("!")
        print()
        print(f"  Error : {e}")
        print(f"  File  : {path}")
        print()
        print("  Your personal config patches will NOT be applied this run.")
        print("  Fix the JSON (or delete the file to regenerate it), or use")
        print("  'Edit config patches' from the main menu to rebuild it.")
        print()
        return []


# ── Config patches: in-script editor ──────────────────────────────────────────

def _coerce_value(text):
    """
    Turn typed input into the right JSON type so a patch works for both line-based
    (.cfg/.properties) and .json targets:
      true/false → bool, whole/decimal numbers → int/float, everything else → str.
    """
    s = text.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return text


def _fmt_value(value):
    """Display a patch value the way it will be written (lowercase booleans)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _print_patch_list(key_patches):
    print(color("  Key patches — change one setting inside the new pack's config:", "bold"))
    if not key_patches:
        print(color("    (none)", "grey"))
    else:
        for i, p in enumerate(key_patches, 1):
            section = color(f"  {{{p['section']}}}", "grey") if p.get("section") else ""
            note = color(f"   # {p['note']}", "grey") if p.get("note") else ""
            print(f"    {color(f'{i}.', 'bcyan')} {p['file']}{section}")
            print(f"       {color(p['key'], 'cyan')} = {color(_fmt_value(p.get('value', '')), 'green')}{note}")
    print()
    print(color("  To copy an entire config, mod, or any other file into your instance,", "grey"))
    print(color("  put it in the 'Additional Files' folder.", "grey"))


def _add_key_patch(key_patches):
    """Prompt for a new patch, append it, and return a status message."""
    print()
    print("  Add a key patch. Paths are relative to .minecraft.")
    print("  Example file: config/GregTech.cfg   Example key: B:EnablePollution")
    file = input("    Config file (blank cancels): ").strip()
    if not file:
        return "Add cancelled."
    key = input("    Key (Forge .cfg needs its prefix, e.g. B:, I:, S:, D:): ").strip()
    if not key:
        return "Add cancelled."
    value = input("    Value to set: ").strip()
    section = input("    Section (optional, Enter to skip): ").strip()
    note = input("    Note for yourself (optional, Enter to skip): ").strip()

    patch = {"file": file, "key": key, "value": _coerce_value(value)}
    if section:
        patch["section"] = section
    if note:
        patch["note"] = note
    key_patches.append(patch)
    return f"✅  Added: {file}  [{key}] = {_fmt_value(patch['value'])}"


def _remove_patch_entry(key_patches):
    """Prompt for a patch number, remove it, and return a status message."""
    print()
    raw = input(f"  Remove which patch? (number 1-{len(key_patches)}, blank cancels): ").strip()
    if not raw:
        return "Nothing removed."
    if not raw.isdigit() or not (1 <= int(raw) <= len(key_patches)):
        return f"No patch numbered '{raw}'."
    removed = key_patches.pop(int(raw) - 1)
    return f"✅  Removed: {removed['file']} [{removed['key']}]"


def preset_detail(preset, presets_state):
    """Show one preset; let the user enable/disable it (and adjust values for the
    adjustable preset). Returns True if anything changed."""
    changed = False
    status = ""
    st = presets_state.setdefault(preset["id"], {"enabled": False})
    if "settings" in preset:
        st.setdefault("values", {s["id"]: s["default"] for s in preset["settings"]})
    if "toggles" in preset:
        st.setdefault("toggles", {t["id"]: t["default"] for t in preset["toggles"]})

    while True:
        screen(preset["name"])
        print(f"  {color(preset['note'], 'grey')}")
        print()
        if "settings" in preset:
            print(f"  {color(preset['file'], 'cyan')}")
            print()
            for i, s in enumerate(preset["settings"], 1):
                val = st["values"].get(s["id"], s["default"])
                option(i, f"{s['label']:<22}{color(str(val), 'green')}")
        if "toggles" in preset:
            print(f"  {color(preset['file'], 'cyan')}")
            print()
            for i, t in enumerate(preset["toggles"], 1):
                tog_on = st["toggles"].get(t["id"], t["default"])
                mark = color("ON ", "bgreen") if tog_on else color("off", "grey")
                option(i, f"{mark}  {t['label']}")
        if preset.get("patches"):
            if "settings" in preset:
                print()
                print(color("  also always sets:", "grey"))
            by_file = {}
            for patch in preset["patches"]:
                by_file.setdefault(patch["file"], []).append(patch)
            for fpath, patches in by_file.items():
                print(f"  {color(fpath, 'cyan')}")
                for patch in patches:
                    print(f"    {color(patch['key'], 'cyan')} = {color(_fmt_value(patch['value']), 'green')}")
        print()
        on = st.get("enabled")
        print("  Status : " + (color("ENABLED", "bgreen") if on else color("disabled", "grey")))
        print()
        if on:
            option("D", "Disable preset")
        else:
            option("E", "Enable preset")
        if "settings" in preset:
            print(color("       (or a number above to change that value)", "grey"))
        if "toggles" in preset:
            print(color("       (or a number above to turn that command on/off)", "grey"))
        option("Q", "Go back")
        print()
        if status:
            print(color(status, "yellow"))
            print()
        raw = choose()

        if raw == "q":
            return changed
        if raw == "e" and not on:
            st["enabled"] = True
            changed = True
            status = "Preset enabled."
        elif raw == "d" and on:
            st["enabled"] = False
            changed = True
            status = "Preset disabled."
        elif "settings" in preset and raw.isdigit() and 1 <= int(raw) <= len(preset["settings"]):
            s = preset["settings"][int(raw) - 1]
            new = input(f"  {s['label']} ({s['min']}-{s['max']}, blank cancels): ").strip()
            if new.isdigit() and s["min"] <= int(new) <= s["max"]:
                st["values"][s["id"]] = int(new)
                changed = True
                status = f"{s['label']} set to {new}."
            elif new:
                status = f"'{new}' isn't a whole number between {s['min']} and {s['max']}."
            else:
                status = ""
        elif "toggles" in preset and raw.isdigit() and 1 <= int(raw) <= len(preset["toggles"]):
            t = preset["toggles"][int(raw) - 1]
            new_val = not st["toggles"].get(t["id"], t["default"])
            st["toggles"][t["id"]] = new_val
            changed = True
            status = f"{t['label']} turned {'ON' if new_val else 'off'}."
        else:
            status = "Choose E/D, a number, or Q."


def manage_presets(presets_state):
    """List presets with their on/off state; open one to toggle/adjust it.
    Returns True if anything changed."""
    changed = False
    status = ""
    while True:
        screen("Manage Presets")
        print("  Enabled presets are applied together with your config patches.")
        print()
        for i, p in enumerate(PRESETS, 1):
            on = presets_state.get(p["id"], {}).get("enabled")
            mark = color("ON ", "bgreen") if on else color("off", "grey")
            option(i, f"{mark}  {p['name']}")
        option("Q", "Go back")
        print()
        if status:
            print(color(status, "yellow"))
            print()
        raw = choose()
        if raw == "q":
            return changed
        if raw.isdigit() and 1 <= int(raw) <= len(PRESETS):
            if preset_detail(PRESETS[int(raw) - 1], presets_state):
                changed = True
            status = ""
        else:
            status = f"Enter a number 1-{len(PRESETS)}, or Q."


def manage_config_patches():
    """
    Editor for config_patches.json: add/remove key patches and toggle presets.
    Reads the current file, lets the user change things, writes back on save.
    """
    path = os.path.join(SCRIPT_DIR, CONFIG_PATCHES_FILE)

    try:
        key_patches, presets = _read_config(path)
    except Exception as e:
        separator("!")
        print("  ⚠️   config_patches.json is not valid JSON.")
        separator("!")
        print()
        print(f"  Error : {e}")
        print()
        ans = input("  Start over with a fresh, empty file? (your current file is "
                    "kept until you save) [y/N]: ").strip().lower()
        if ans != "y":
            print("  Left the file untouched.")
            return
        key_patches, presets = [], _default_presets_state()

    dirty = False
    status = ""
    while True:
        screen("Config Patches")
        _print_patch_list(key_patches)
        print()
        enabled = [p["name"] for p in PRESETS if presets.get(p["id"], {}).get("enabled")]
        if enabled:
            print(color("  Presets enabled: ", "bold") + color(", ".join(enabled), "green"))
        else:
            print(color("  Presets enabled: ", "bold") + color("none", "grey"))
        print()
        if status:
            tone = "green" if status.startswith("✅") else "yellow"
            print(f"  {color(status, tone)}")
            print()
        option("A", "Add patch")
        option("R", "Remove patch")
        option("P", "Manage presets")
        option("S", "Save & back")
        option("Q", "Back without saving")
        print()
        choice = choose()

        if choice == "a":
            before = len(key_patches)
            status = _add_key_patch(key_patches)
            if len(key_patches) != before:
                dirty = True
        elif choice == "r":
            if not key_patches:
                status = "Nothing to remove."
            else:
                before = len(key_patches)
                status = _remove_patch_entry(key_patches)
                if len(key_patches) != before:
                    dirty = True
        elif choice == "p":
            if manage_presets(presets):
                dirty = True
                status = "Presets updated."
        elif choice == "s":
            _write_config(path, key_patches, presets)
            clear_screen()
            print()
            print(color(f"  ✅  Saved {len(key_patches)} key patch(es) and "
                        f"{len(enabled)} preset(s) enabled.", "green"))
            print(color("      (Help text & examples kept; manual // comments are not.)", "grey"))
            print()
            input("  Press Enter to return to the menu...")
            return
        elif choice == "q":
            if dirty:
                ans = input("  Discard your unsaved changes? [y/N]: ").strip().lower()
                if ans != "y":
                    continue
            return
        else:
            status = "Please choose A, R, P, S, or Q."


# ── Step 1: Backup warning ─────────────────────────────────────────────────────

def confirm_backup(zip_name, key_patches):
    clear_screen()
    separator("═")
    print(color("  ⚠️   BACK UP YOUR INSTANCE BEFORE PROCEEDING!", "byellow", "bold"))
    separator("═")
    print()
    print(f"  Instance  : {color(os.path.basename(INSTANCE_DIR), 'bcyan')}")
    print(f"  Pack file : {color(zip_name, 'bcyan')}")
    print()
    print(color("  REPLACED", "bred") + " with the new pack (your old version is deleted):")
    print("    patches, libraries, mods, config, scripts, serverutilities, .lang")
    print()
    print(color("  PRESERVED", "bgreen") + " from your install (kept, never overwritten):")
    print("    saves, schematics, screenshots, maps, journeymap, visualprospecting,")
    print("    TCNodeTracker, shaderpacks, resourcepacks, opencomputers, options*.txt,")
    print("    servers.dat, localconfig.cfg, BotaniaVars.dat, config/NEI,")
    print("    config/journeymap, config/shaders.properties, config/vendingmachine")
    print(color("    (resourcepacks: yours kept; new ones from the pack added on top)", "grey"))
    print()
    if key_patches:
        print(color("  PATCHED", "bcyan") + f": {len(key_patches)} config patch(es) from config_patches.json re-applied.")
        print()
    print(color("  If it fails, a rollback restores everything. Still, keep your own backup.", "grey"))
    print()
    answer = input(color("  Type YES to proceed (anything else cancels): ", "bold")).strip()
    if answer != "YES":
        raise _Cancelled()
    print()


# ── Step (1/8): Extract zip ─────────────────────────────────────────────────────

def _extract_with_progress(zf, dest):
    """Extract a ZipFile to dest, showing a live progress bar when interactive.
    When output isn't a terminal (piped/logged), extracts quietly and fast."""
    if not sys.stdout.isatty():
        zf.extractall(dest)
        return
    members = zf.infolist()
    total = sum(mi.file_size for mi in members) or 1
    done = 0
    start = time.time()
    for i, mi in enumerate(members, 1):
        zf.extract(mi, dest)
        done += mi.file_size
        if i % 30 == 0 or i == len(members):
            pct = int(done * 100 / total)
            filled = pct // 4
            elapsed = time.time() - start
            speed = (done / elapsed / 1048576) if elapsed > 0 else 0
            bar = color("█" * filled, "bgreen") + color("─" * (25 - filled), "grey")
            print(f"\r  [{bar}] {color(f'{pct:3d}%', 'bcyan')}  "
                  f"{done/1048576:7.1f}/{total/1048576:.1f} MB  {speed:4.1f} MB/s",
                  end="", flush=True)
    print()


def extract_zip(zip_path):
    extract_dir = os.path.join(SCRIPT_DIR, "_extracted")
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)

    print(color(f"[1/{TOTAL_STEPS}]", "bcyan") + f" Extracting {os.path.basename(zip_path)}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        _extract_with_progress(zf, extract_dir)

    # ── Double-zip detection ──────────────────────────────────────────────────
    # Some distributions wrap the pack inside a second zip.
    # If the outer zip contains only a single .zip file (and no folders),
    # extract that inner zip automatically and work from its contents instead.
    top_level_items = os.listdir(extract_dir)
    inner_zips = [
        f for f in top_level_items
        if f.lower().endswith(".zip") and os.path.isfile(os.path.join(extract_dir, f))
    ]
    top_level_dirs = [
        d for d in top_level_items
        if os.path.isdir(os.path.join(extract_dir, d))
    ]

    if inner_zips and not top_level_dirs:
        inner_zip_path = os.path.join(extract_dir, inner_zips[0])
        print(f"  Double-zip detected — extracting inner zip: {inner_zips[0]}")
        inner_extract_dir = os.path.join(extract_dir, "_inner")
        with zipfile.ZipFile(inner_zip_path, "r") as zf:
            _extract_with_progress(zf, inner_extract_dir)
        os.remove(inner_zip_path)
        # Re-scan from the inner extraction
        top_level_dirs = [
            d for d in os.listdir(inner_extract_dir)
            if os.path.isdir(os.path.join(inner_extract_dir, d))
        ]
        scan_dir = inner_extract_dir
    else:
        scan_dir = extract_dir

    # ── Locate pack root ──────────────────────────────────────────────────────
    # Release packs extract to a single wrapper folder that holds .minecraft
    # (e.g. "GT_New_Horizons_2.8.4/"). Daily/experimental artifacts may instead
    # put .minecraft straight at the top level. Handle both.
    if ".minecraft" in top_level_dirs:
        pack_root = scan_dir
        label = os.path.splitext(os.path.basename(zip_path))[0]
    elif len(top_level_dirs) == 1:
        pack_root = os.path.join(scan_dir, top_level_dirs[0])
        label = top_level_dirs[0]
    else:
        shutil.rmtree(extract_dir)
        raise RuntimeError(
            f"Could not find the pack root in the zip (top-level items: {top_level_dirs}).\n"
            "Please make sure you're using the correct GT:NH client pack zip."
        )

    print(f"  Pack version detected: {label}")
    return extract_dir, pack_root


# ── Step (2/8): Snapshot old folders aside (rollback) ──────────────────────────

def _rollback_items():
    """Return (label, base_dir, name) for every folder we replace."""
    items = []
    for name in ["patches", "libraries"]:
        items.append(("instance", INSTANCE_DIR, name))
    for name in ["mods", "config", "scripts", "serverutilities"]:
        items.append(("minecraft", MINECRAFT_DIR, name))
    return items


def snapshot_and_remove():
    """
    Move the to-be-replaced folders into ROLLBACK_DIR instead of deleting them.
    They serve as both the rollback snapshot and the source for personal-file
    restoration. Discarded by commit_rollback() once the update succeeds.
    """
    print("\n" + color(f"[2/{TOTAL_STEPS}]", "bcyan") + " Snapshotting & removing old files...")

    if os.path.isdir(ROLLBACK_DIR):
        shutil.rmtree(ROLLBACK_DIR)

    for label, base, name in _rollback_items():
        src = os.path.join(base, name)
        if os.path.exists(src):
            dst = os.path.join(ROLLBACK_DIR, label, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print(f"  Moved aside : {name}")
        else:
            print(f"  Skipped (not found): {name}")

    # .lang files in .minecraft root
    if os.path.isdir(MINECRAFT_DIR):
        lang_dst = os.path.join(ROLLBACK_DIR, "minecraft_lang")
        moved = 0
        for fname in os.listdir(MINECRAFT_DIR):
            if fname.endswith(".lang") and os.path.isfile(os.path.join(MINECRAFT_DIR, fname)):
                os.makedirs(lang_dst, exist_ok=True)
                shutil.move(os.path.join(MINECRAFT_DIR, fname), os.path.join(lang_dst, fname))
                moved += 1
        if moved:
            print(f"  Moved aside : {moved} .lang file(s)")


def restore_rollback():
    """Move the snapshot folders back, discarding any partially-written new files."""
    if not os.path.isdir(ROLLBACK_DIR):
        return

    for label, base, name in _rollback_items():
        snap = os.path.join(ROLLBACK_DIR, label, name)
        if os.path.exists(snap):
            dst = os.path.join(base, name)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(snap, dst)

    lang_dst = os.path.join(ROLLBACK_DIR, "minecraft_lang")
    if os.path.isdir(lang_dst):
        for fname in os.listdir(lang_dst):
            target = os.path.join(MINECRAFT_DIR, fname)
            if os.path.exists(target):
                os.remove(target)
            shutil.move(os.path.join(lang_dst, fname), target)

    shutil.rmtree(ROLLBACK_DIR, ignore_errors=True)


def commit_rollback():
    """Discard the snapshot after a successful update."""
    if os.path.isdir(ROLLBACK_DIR):
        shutil.rmtree(ROLLBACK_DIR)


# ── Step (3/8): Copy new folders from zip ──────────────────────────────────────

def copy_folder_replace(src, dst):
    if not os.path.isdir(src):
        print(f"  WARNING: Not found in zip, skipping: {os.path.basename(src)}")
        return
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  Copied  : {os.path.basename(src)}")


def copy_folder_merge_skip(src, dst):
    """Copy src into dst, skipping files that already exist."""
    if not os.path.isdir(src):
        print(f"  WARNING: Not found in zip, skipping: {os.path.basename(src)}")
        return
    copied = skipped = 0
    for root, dirs, files in os.walk(src):
        rel_root  = os.path.relpath(root, src)
        dest_root = os.path.join(dst, rel_root)
        os.makedirs(dest_root, exist_ok=True)
        for fname in files:
            dest_file = os.path.join(dest_root, fname)
            if not os.path.exists(dest_file):
                shutil.copy2(os.path.join(root, fname), dest_file)
                copied += 1
            else:
                skipped += 1
    print(f"  Merged  : {os.path.basename(src)}  ({copied} new, {skipped} already existed → kept)")


def copy_lang_files(pack_root):
    src_mc = os.path.join(pack_root, ".minecraft")
    if not os.path.isdir(src_mc):
        return
    copied = 0
    for fname in os.listdir(src_mc):
        if fname.endswith(".lang"):
            shutil.copy2(os.path.join(src_mc, fname), os.path.join(MINECRAFT_DIR, fname))
            copied += 1
    if copied:
        print(f"  Copied  : {copied} .lang file(s)")


def copy_new_files(pack_root):
    print("\n" + color(f"[3/{TOTAL_STEPS}]", "bcyan") + " Copying new files from pack...")

    minecraft_src = os.path.join(pack_root, ".minecraft")

    # Make sure the .minecraft folder exists (it may have only held wiped folders)
    os.makedirs(MINECRAFT_DIR, exist_ok=True)

    # Instance root
    for folder in ["libraries", "patches"]:
        copy_folder_replace(os.path.join(pack_root, folder), os.path.join(INSTANCE_DIR, folder))

    # .minecraft — full replace
    for folder in ["mods", "config", "scripts", "serverutilities"]:
        copy_folder_replace(os.path.join(minecraft_src, folder), os.path.join(MINECRAFT_DIR, folder))

    # .lang files in .minecraft root
    copy_lang_files(pack_root)

    # resourcepacks — preserved: your existing packs are kept, and any new ones
    # shipped with this pack version are added on top (existing files never replaced)
    copy_folder_merge_skip(
        os.path.join(minecraft_src, "resourcepacks"),
        os.path.join(MINECRAFT_DIR, "resourcepacks")
    )


# ── Step (4/8): Restore preserved personal files ───────────────────────────────

def restore_personal_files():
    """
    Copy preserved personal files (CLIENT_PRESERVE) from the rollback snapshot back
    into the freshly installed pack, so your settings survive the wipe. Items that
    weren't part of a replaced folder (so they were never moved) are simply skipped
    — they already survived in place.
    """
    print("\n" + color(f"[4/{TOTAL_STEPS}]", "bcyan") + " Restoring preserved personal files...")

    snap_mc = os.path.join(ROLLBACK_DIR, "minecraft")
    restored = 0

    for rel in CLIENT_PRESERVE:
        relpath = rel.replace("/", os.sep)
        src = os.path.join(snap_mc, relpath)
        if not os.path.exists(src):
            continue
        dst = os.path.join(MINECRAFT_DIR, relpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        restored += 1
        print(f"  Restored : {rel}")

    if restored == 0:
        print("  Nothing to restore (no preserved files found from the previous version).")
    else:
        print(f"  ✅  Restored {restored} item(s).")


# ── Step (5/8): Apply config patches ───────────────────────────────────────────

def _atomic_write(path, text, newline):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _find_key_in_lines(lines, key, section="", sep="="):
    """
    Return the index of the line defining `key`, optionally scoped to a top-level
    Forge section block. Mirrors the section/brace-aware matching used by the
    PowerShell updater. `sep` is the key/value separator ("=" for Forge .cfg /
    .properties, ":" for ServerUtilities ranks.txt). Returns -1 if not found.
    """
    if not lines:
        return -1

    pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*' + re.escape((sep.strip() or "=")[:1]))
    in_target = (section == "")
    depth = 0

    for i, line in enumerate(lines):
        # Comment lines never affect section tracking or key matching
        if re.match(r'^\s*[#/]', line):
            continue

        m = re.match(r'^\s*"?([^"{}=#]+)"?\s*\{', line)
        if m:
            depth += 1
            if depth == 1:
                current = m.group(1).strip()
                if section:
                    in_target = (current == section)
        elif re.match(r'^\s*\}', line) and section:
            if depth > 0:
                depth -= 1
            if depth == 0:
                in_target = False

        if in_target and pattern.match(line):
            return i

    return -1


def _patch_keyvalue(path, key, value, section="", sep="=", all_occ=False):
    """
    Patch a line-based config. `sep` is the key/value separator written back
    ("=" for Forge .cfg / .properties, ": " for ServerUtilities ranks.txt).
    With all_occ=True every matching line is updated (used for ranks.txt, where
    the same setting can appear under multiple ranks); otherwise the first match
    (optionally section-scoped) is updated.
    """
    if isinstance(value, bool):
        sval = "true" if value else "false"
    else:
        sval = str(value)

    # newline="" keeps raw CR/LF so the line-ending detection below is accurate;
    # without it, universal-newline mode would hide \r\n and we'd rewrite the
    # whole file as LF instead of only changing the patched line.
    with open(path, "r", encoding="utf-8", newline="") as f:
        content = f.read()
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()

    if all_occ:
        pat = re.compile(r'^\s*' + re.escape(key) + r'\s*' + re.escape((sep.strip() or "=")[:1]))
        hits = [i for i, line in enumerate(lines) if pat.match(line)]
        if not hits:
            return False
        for i in hits:
            lead = re.match(r'^(\s*)', lines[i]).group(1)
            lines[i] = f"{lead}{key}{sep}{sval}"
    else:
        idx = _find_key_in_lines(lines, key, section, sep)
        if idx < 0:
            return False
        lead = re.match(r'^(\s*)', lines[idx]).group(1)
        lines[idx] = f"{lead}{key}{sep}{sval}"

    out = newline.join(lines)
    if content.endswith(("\n", "\r")):
        out += newline
    _atomic_write(path, out, newline)
    return True


def _patch_json(path, dotted_key, value):
    """Patch a .json config by dotted key path (e.g. ranks.default.enabled)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = dotted_key.split(".")
    node = data
    for k in keys[:-1]:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return False

    last = keys[-1]
    if not isinstance(node, dict) or last not in node:
        return False

    node[last] = value
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(path, text, "\n")
    return True


def apply_config_patches(key_patches, tag=None):
    if tag is None:
        tag = color(f"[5/{TOTAL_STEPS}]", "bcyan")
    print("\n" + tag + " Applying config patches...")

    if not key_patches:
        print("  No config patches defined — skipping.")
        return

    applied = failed = 0
    for patch in key_patches:
        rel     = patch["file"]
        key     = patch["key"]
        value   = patch.get("value", "")
        section = patch.get("section", "") or ""
        note    = patch.get("note", "")

        target = os.path.join(MINECRAFT_DIR, rel.replace("/", os.sep))
        label  = f"{rel} [{key}]" + (f"  ({note})" if note else "")

        if not os.path.isfile(target):
            print(f"  ⚠️  Skipped (file not found): {label}")
            failed += 1
            continue

        try:
            if rel.lower().endswith(".json"):
                ok = _patch_json(target, key, value)
            else:
                ok = _patch_keyvalue(target, key, value, section,
                                     sep=patch.get("sep", "="), all_occ=bool(patch.get("all")))
        except Exception as e:
            print(f"  ⚠️  Error patching {label}: {e}")
            failed += 1
            continue

        if ok:
            print(f"  Patched : {label} = {value}")
            applied += 1
        else:
            where = f" in section '{section}'" if section else ""
            print(f"  ⚠️  Key not found{where}: {label}")
            failed += 1

    print(f"  ✅  {applied} patch(es) applied" + (f", {failed} not applied" if failed else "") + ".")


# ── Version conflict detection ────────────────────────────────────────────────


_VERSION_KEYWORDS = ('alpha', 'beta', 'rc', 'pre', 'release', 'final',
                     'forge', 'fabric', 'universal', 'dev')


def _is_version_token(part):
    """True if a '-'/'_' separated filename token starts the version part
    (e.g. '1.7.10', 'MC1.7.10', 'v2.0', or a keyword like 'beta')."""
    return bool(re.match(r'^v?(mc)?\d', part, re.IGNORECASE)) or part.lower() in _VERSION_KEYWORDS


def mod_base_name(filename):
    """
    Strip version tokens from a mod filename to get a comparable base name.
    Handles common patterns like:
      angelica-1.0.0-beta66b.jar       -> angelica
      BetterFoliage-MC1.7.10-2.0.17.jar -> betterfoliage
      journeymap-1.7.10-5.1.4p2.jar    -> journeymap
    """
    name = os.path.splitext(filename)[0]
    base_parts = []
    for part in re.split(r'[-_]', name):
        if _is_version_token(part):
            break
        base_parts.append(part.lower())
    return '-'.join(base_parts) if base_parts else name.lower()


def _mod_version_nums(filename):
    """
    Numeric version tuple from a mod filename, for comparing two builds of the
    same mod. The Minecraft-version token (1.7.10 / mc1.7.10) is dropped since it
    is shared noise. Returns an empty tuple when no version can be read.
    """
    name = os.path.splitext(filename)[0]
    started = False
    tokens = []
    for part in re.split(r'[-_]', name):
        if not started:
            started = _is_version_token(part)
        if started and not re.fullmatch(r'(mc)?1\.7\.10', part, re.IGNORECASE):
            tokens.append(part)
    return tuple(int(n) for n in re.findall(r'\d+', '-'.join(tokens)))


def version_status(incoming_file, installed_file):
    """How the incoming mod's version compares to the installed one:
    'newer', 'older', 'same', or 'unknown' (can't tell from the filenames)."""
    a = _mod_version_nums(incoming_file)
    b = _mod_version_nums(installed_file)
    if not a or not b:
        return "unknown"
    if a == b:
        return "same"
    return "newer" if a > b else "older"


def fuzzy_mod_name(filename):
    """Aggressively normalised mod name for duplicate detection across naming styles."""
    base = mod_base_name(filename)
    base = re.sub(r"[’']s\b", "s", base)
    base = base.replace("-s-", "s")
    base = re.sub(r'[\s\-_.+]', '', base).lower()
    if len(base) > 5 and base.endswith('s') and not re.search(r'(ss|ps|ns|us|is|as)$', base):
        base = base[:-1]
    return base


def find_mod_conflicts(extra_files, additional_mods_dir):
    """
    Find Additional Files mods that clash with an already-installed mod (same base
    name, different filename). Returns a list of dicts:
      {name, incoming, installed, status}  (status from version_status()).
    """
    instance_mods_dir = os.path.join(MINECRAFT_DIR, 'mods')
    if not os.path.isdir(instance_mods_dir):
        return []

    existing = {}
    for fname in os.listdir(instance_mods_dir):
        if fname.lower().endswith(('.jar', '.zip', '.disabled')):
            base = mod_base_name(fname)
            if base:
                existing[base] = fname

    conflicts = []
    for src_file in extra_files:
        try:
            if os.path.commonpath([src_file, additional_mods_dir]) != additional_mods_dir:
                continue
        except ValueError:
            continue
        fname = os.path.basename(src_file)
        if not fname.lower().endswith(('.jar', '.zip', '.disabled')):
            continue
        base = mod_base_name(fname)
        if base in existing and existing[base] != fname:
            conflicts.append({
                "name": base,
                "incoming": fname,
                "installed": existing[base],
                "status": version_status(fname, existing[base]),
            })
    return conflicts


# ── Step (6/8): Additional Files ───────────────────────────────────────────────

def handle_additional_files(tag=None):
    additional_dir = os.path.join(SCRIPT_DIR, "Additional Files")
    mods_dir       = os.path.join(additional_dir, "mods")

    just_created = not os.path.isdir(additional_dir)
    os.makedirs(additional_dir, exist_ok=True)

    if tag is None:
        tag = color(f"[6/{TOTAL_STEPS}]", "bcyan")
    print("\n" + tag + " Checking Additional Files folder...")

    def list_files(directory):
        found = []
        for root, _, files in os.walk(directory):
            for f in files:
                found.append(os.path.join(root, f))
        return found

    # Scan everything inside Additional Files, not just mods/config
    extra_files = list_files(additional_dir)

    if just_created or not extra_files:
        print()
        separator()
        print("  📂  The 'Additional Files' folder is empty (or was just created).")
        separator()
        print()
        print("  Place any files or folders here that you want copied into .minecraft")
        print("  after every update. The folder structure is preserved exactly.")
        print(f"  Location: {additional_dir}")
        print()
        print("  Example layout:")
        print("    Additional Files/")
        print("    ├── mods/         → BetterFoliage-MC1.7.10-2.0.17.jar")
        print("    ├── config/       → BetterFoliage.cfg")
        print("    ├── resourcepacks/ → MyTexturePack.zip")
        print("    └── shaderpacks/  → Sildurs.zip")
        print()
        print("  Next time you run the script, it will offer to copy these automatically.")
        print()
        print("  ℹ️   No additional files found — skipping.")
        return []

    print(f"  Found {len(extra_files)} file(s) in 'Additional Files':")
    for f in extra_files:
        print(f"    + {os.path.relpath(f, additional_dir)}")
    print()

    # A mod here may already be installed under a different version. Importing it
    # then leaves two versions in mods/ (a crash) unless the old one is disabled.
    conflicts = find_mod_conflicts(extra_files, mods_dir)
    skip_incoming = set()   # incoming filenames NOT to copy (older downgrades)
    disable_installed = []  # installed filenames to rename to .disabled

    if conflicts:
        status_tag = {
            "older":   color("  ← OLDER than installed (a downgrade)", "bred"),
            "newer":   color("  ← newer than installed", "green"),
            "same":    color("  ← same version, different file", "grey"),
            "unknown": color("  ← can't tell which is newer", "grey"),
        }
        separator("!")
        print(color("  ⚠️   Some of these mods are already installed", "byellow"))
        separator("!")
        print()
        for c in conflicts:
            print(f"  {c['name']}")
            print(f"    Installed : {c['installed']}")
            print(f"    Importing : {c['incoming']}{status_tag[c['status']]}")
            print()

        downgrades = [c for c in conflicts if c["status"] == "older"]
        if downgrades:
            print(color(f"  {len(downgrades)} import(s) would replace a NEWER installed mod with an", "yellow"))
            print(color("  older one. Option 1 keeps your newer versions.", "yellow"))
            print()

        print("  Each imported mod needs the installed copy removed, or you'll run")
        print("  two versions at once. How should I handle the clashes?")
        print()
        option("1", "Import newer/unknown, skip older — disable the ones I replace" + color("  (recommended)", "grey"))
        option("2", "Import all (downgrade older too) — disable the ones I replace")
        option("3", "Import all, keep both versions — you delete the old ones yourself")
        option("Q", "Skip Additional Files entirely")
        print()
        ans = choose(default=1)
        print()

        if ans == "q":
            print("  ⏭️   Skipping Additional Files.")
            return []
        if ans == "3":
            pass  # keep both: import everything, disable nothing
        elif ans == "2":
            disable_installed = [c["installed"] for c in conflicts]
        else:  # "1" / "" → skip older, replace the rest
            for c in conflicts:
                if c["status"] == "older":
                    skip_incoming.add(c["incoming"])
                else:
                    disable_installed.append(c["installed"])
    else:
        option("Y", "Copy them into .minecraft")
        option("N", "Skip")
        print()
        if choose(default="Y") == "n":
            print("  ⏭️   Skipping Additional Files. You can copy them manually later.")
            return []
        print()

    # Disable the installed versions we're replacing, so there's no duplicate.
    if disable_installed:
        instance_mods_dir = os.path.join(MINECRAFT_DIR, "mods")
        for old_mod in disable_installed:
            old_path = os.path.join(instance_mods_dir, old_mod)
            if os.path.isfile(old_path):
                os.rename(old_path, old_path + ".disabled")
                print(f"  Disabled : {old_mod} → {old_mod}.disabled")

    copied = skipped = 0
    copied_mods = []
    for src_file in extra_files:
        if os.path.basename(src_file) in skip_incoming:
            skipped += 1
            continue
        rel       = os.path.relpath(src_file, additional_dir)
        dest_file = os.path.join(MINECRAFT_DIR, rel)
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        shutil.copy2(src_file, dest_file)
        copied += 1
        try:
            if os.path.commonpath([src_file, mods_dir]) == mods_dir:
                copied_mods.append(os.path.basename(src_file))
        except ValueError:
            pass

    msg = color(f"  ✅  Copied {copied} file(s) into .minecraft.", "green")
    if skipped:
        msg += color(f"  Skipped {skipped} older mod(s).", "grey")
    print(msg)
    return copied_mods


def list_installed_mods(copied_mods):
    if not copied_mods:
        return

    mods = sorted(copied_mods)

    print()
    separator()
    print(f"  📦  {len(mods)} mod(s) added from 'Additional Files':")
    separator()
    for mod in mods:
        print(f"    • {mod}")
    separator()


# ── Step (7/8): Post-update verification ───────────────────────────────────────

def verify_instance():
    print("\n" + color(f"[7/{TOTAL_STEPS}]", "bcyan") + " Verifying instance...")

    mods_dir = os.path.join(MINECRAFT_DIR, "mods")
    warnings = []

    # Critical directories
    for name, base in [("mods", MINECRAFT_DIR), ("config", MINECRAFT_DIR), ("libraries", INSTANCE_DIR)]:
        if not os.path.isdir(os.path.join(base, name)):
            warnings.append(f"{name}/ directory is MISSING")

    jars = []
    if os.path.isdir(mods_dir):
        jars = [f for f in os.listdir(mods_dir)
                if f.lower().endswith(".jar") and os.path.isfile(os.path.join(mods_dir, f))]
    mod_count = len(jars)

    # Mod count sanity
    if os.path.isdir(mods_dir) and mod_count < 150:
        warnings.append(f"Only {mod_count} mods found (GTNH typically has 250+). Update may be incomplete.")

    # GregTech core mod present
    if jars and not any(("gregtech" in j.lower() or "gt5" in j.lower()) for j in jars):
        warnings.append("GregTech JAR not found — this may not be a valid GTNH instance.")

    # Zero-byte (corrupted) jars
    empties = [j for j in jars if os.path.getsize(os.path.join(mods_dir, j)) == 0]
    if empties:
        warnings.append(f"Found {len(empties)} empty/corrupted JAR file(s):")
        for j in empties:
            warnings.append(f"    * {j} (0 bytes)")
        warnings.append("These will cause class-not-found errors. Delete and re-download them.")

    # Duplicate mods (same fuzzy base name, different versions)
    fuzzy = {}
    for j in jars:
        fuzzy.setdefault(fuzzy_mod_name(j), []).append(j)
    dupes = {k: v for k, v in fuzzy.items() if len(v) > 1}
    if dupes:
        warnings.append(f"Found {len(dupes)} mod(s) with multiple versions:")
        for v in dupes.values():
            warnings.append(f"    * {', '.join(sorted(v))}")
        warnings.append("Multiple versions of the same mod will crash the game. Remove the older one(s).")

    # Cross-folder duplicates: mods/1.7.10/ vs mods/
    sub_dir = os.path.join(mods_dir, "1.7.10")
    if os.path.isdir(sub_dir):
        sub_jars = [f for f in os.listdir(sub_dir) if f.lower().endswith(".jar")]
        main_fuzzy = {fuzzy_mod_name(j) for j in jars}
        cross = []
        for sj in sub_jars:
            if fuzzy_mod_name(sj) in main_fuzzy:
                cross.append(sj)
        if cross:
            warnings.append(f"Found {len(cross)} cross-folder duplicate(s) (mods/1.7.10/ vs mods/):")
            for c in cross:
                warnings.append(f"    * {c}")

    if not warnings:
        print(color(f"  ✅  Verification passed ({mod_count} mods, no issues).", "green"))
        return

    print()
    separator("!")
    print(color("  ⚠️   Verification found issues:", "byellow"))
    separator("!")
    for w in warnings:
        print(f"  {w}")
    print()
    print("  The update was applied — review the warnings above before launching.")


# ── Step (8/8): Cleanup ─────────────────────────────────────────────────────────

def cleanup(extract_dir):
    print("\n" + color(f"[8/{TOTAL_STEPS}]", "bcyan") + " Cleaning up temporary files...")
    if extract_dir and os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)
    print("  Done.")


# ── Main ───────────────────────────────────────────────────────────────────────

def run_update():
    # 0a. Select instance (sets INSTANCE_DIR / MINECRAFT_DIR globals)
    select_instance()

    # 0b. Optionally make a full backup of the instance before anything changes.
    offer_full_backup()

    # 0c. Obtain the pack zip (download from the site or use a local file).
    #     Nothing destructive happens here.
    zip_path = obtain_zip()

    # 0c. Load personal config patches (warns early if malformed)
    key_patches = load_config_patches()

    # 1. Require backup confirmation
    confirm_backup(os.path.basename(zip_path), key_patches)

    # 2. Extract zip to temp folder (nothing destructive yet)
    extract_dir, pack_root = extract_zip(zip_path)

    # ── Critical phase: the actual file replacement, protected by the rollback
    #    snapshot. A failure OR an interrupt (Ctrl+C) here restores the instance.
    snapshot_taken = False
    try:
        snapshot_and_remove()           # 3. move old folders into the snapshot
        snapshot_taken = True
        copy_new_files(pack_root)        # 4. copy new files from the pack
        restore_personal_files()         # 5. restore preserved personal files
        apply_config_patches(key_patches)  # 6. apply config patches
    except _Cancelled:
        raise                            # cancel is never a failed update
    except BaseException as e:
        interrupted = isinstance(e, (KeyboardInterrupt, SystemExit))
        if interrupted:
            print(color("\n\n  Interrupted — rolling back your instance...", "yellow"))
        else:
            print(color(f"\n❌  Update failed: {e}", "bred"))
        if snapshot_taken:
            try:
                restore_rollback()
                print(color("  ✅  Rolled back — your instance is back to its previous version.", "green"))
            except Exception as rollback_error:
                print(color(f"  ⚠️  Rollback failed: {rollback_error}", "yellow"))
                print(f"      Your previous files are preserved in:")
                print(f"      {ROLLBACK_DIR}")
                print(f"      Move them back manually before launching the game.")
        cleanup(extract_dir)
        if interrupted:
            raise                        # let the top-level handler exit cleanly
        return

    # Core files are in place — discard the snapshot before the optional extras.
    commit_rollback()

    # ── Post phase: optional extras. The core update is already committed, so a
    #    problem here is reported but does NOT roll back the update.
    try:
        copied_mods = handle_additional_files()  # 7. personal mods/configs
        list_installed_mods(copied_mods)
        verify_instance()                        # 8. verify the result
    except _Cancelled:
        raise
    except Exception as e:
        print(color(f"\n  ⚠️  Update applied, but an extra step had a problem: {e}", "yellow"))
    finally:
        cleanup(extract_dir)

    print()
    separator("═")
    print(color("  ✅  Update complete! You're ready to launch GT: New Horizons.", "bgreen"))
    separator("═")
    print()


def run_apply():
    """Apply config patches and/or Additional Files to an instance — no pack update."""
    # 1. Select instance (sets INSTANCE_DIR / MINECRAFT_DIR globals)
    select_instance()

    # 2. Choose what to apply
    status = ""
    while True:
        screen("Apply patches & files")
        print(f"  Instance : {color(os.path.basename(os.path.normpath(INSTANCE_DIR)), 'bgreen')}")
        print()
        print("  Apply to this instance without doing a full pack update:")
        print()
        option("1", "Config patches only")
        option("2", "Additional Files only")
        option("3", "Both")
        option("Q", "Go back")
        print()
        if status:
            print(color(status, "yellow"))
            print()
        what = choose(default=3)
        print()
        if what == "q":
            raise _Cancelled()
        if what in ("", "3"):
            do_patches = do_files = True
            break
        if what == "1":
            do_patches, do_files = True, False
            break
        if what == "2":
            do_patches, do_files = False, True
            break
        status = "Please choose 1, 2, 3, or Q."

    # 3. Optional full backup first (defaults to skip)
    offer_full_backup()

    # 4. Apply the selected pieces (no snapshot/wipe — these are additive edits).
    total = (1 if do_patches else 0) + (1 if do_files else 0)
    n = 0
    if do_patches:
        n += 1
        key_patches = load_config_patches()
        apply_config_patches(key_patches, tag=color(f"[{n}/{total}]", "bcyan"))
    if do_files:
        n += 1
        copied_mods = handle_additional_files(tag=color(f"[{n}/{total}]", "bcyan"))
        list_installed_mods(copied_mods)

    done = " and ".join(
        ([color("config patches", "green")] if do_patches else [])
        + ([color("Additional Files", "green")] if do_files else [])
    )
    print()
    separator("═")
    print(color("  ✅  Done — ", "bgreen") + done + color(" applied.", "bgreen"))
    separator("═")
    print()


def main():
    status = ""
    while True:
        clear_screen()
        print_banner()
        print()
        separator("═")
        print("       " + color("GT: New Horizons — Instance Updater", "bold"))
        separator("═")
        print()
        option("1", "Update an instance")
        option("2", "Apply patches & files to instance")
        option("3", "Edit config patches")
        option("Q", "Quit")
        print()
        if status:
            print(f"  {color(status, 'yellow')}")
            print()
        choice = choose(default=1)

        if choice in ("", "1"):
            clear_screen()
            try:
                run_update()
            except _Cancelled:
                status = "Update cancelled — nothing was changed."
                continue
            return
        elif choice == "2":
            clear_screen()
            try:
                run_apply()
            except _Cancelled:
                status = "Cancelled — nothing was changed."
                continue
            return
        elif choice == "3":
            manage_config_patches()
            status = ""
        elif choice == "q":
            print("\n  Bye!")
            return
        else:
            status = "Please choose 1, 2, 3, or Q."


def _safe_pause():
    """Final 'Press Enter to exit' that itself tolerates Ctrl+C / Ctrl+D."""
    try:
        input("\nPress Enter to exit...")
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    enable_color()
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C / Ctrl+D anywhere — exit cleanly, never a traceback.
        print(color("\n\n  Interrupted — exiting.", "yellow"))
        _safe_pause()
        sys.exit(130)
    except Exception as e:
        print(color(f"\n❌  Unexpected error: {e}", "bred"))
        _safe_pause()
        sys.exit(1)
    _safe_pause()
