# GT: New Horizons — Instance Updater

A single-file, menu-driven updater for **client** [GregTech: New Horizons](https://www.gtnewhorizons.com/)
instances managed with **Prism Launcher** (or any MultiMC-style launcher). It
updates a `.minecraft` instance to a new pack version while **preserving your
worlds, waypoints, settings, and personal mods** — and can re-apply config tweaks
automatically every time.

```
 ██████╗ ████████╗███╗   ██╗██╗  ██╗
██╔════╝ ╚══██╔══╝████╗  ██║██║  ██║
██║  ███╗   ██║   ██╔██╗ ██║███████║
██║   ██║   ██║   ██║╚██╗██║██╔══██║
╚██████╔╝   ██║   ██║ ╚████║██║  ██║
 ╚═════╝    ╚═╝   ╚═╝  ╚═══╝╚═╝  ╚═╝
              By Willshaper
```

> ⚠️ **Client-only.** This tool never touches server files. Always keep a backup
> of important worlds — the built-in backup/rollback help, but they're not a
> substitute for your own backups.

## Requirements

- **Windows 10/11** with **Prism Launcher** (MultiMC / PolyMC also work).
- **Python 3.7+** — [python.org/downloads](https://www.python.org/downloads/)
  (tick *"Add python.exe to PATH"* during install).
- **No other dependencies.** Standard library only — no `pip install` needed.

## Getting started

1. Download this repo (green **Code → Download ZIP**, then extract) or `git clone` it.
2. Double-click **`Launch-GTNH-Updater.bat`**.
   *(On Linux/macOS run `./Launch-GTNH-Updater.sh` — but see [Other platforms](#other-platforms).)*

That's it. The menu walks you through everything.

## The menu

```
    [1] Update an instance
    [2] Apply patches & files to instance
    [3] Edit config patches
    [Q] Quit
```

### [1] Update an instance
1. Pick your GTNH instance (auto-detected by its mods, so other GregTech packs
   are ignored).
2. Optionally make a full backup first (a copy of the whole instance, added to a
   *Backup* group in Prism).
3. Choose where to get the pack:
   - **Stable** — pick any official release from the version-history page.
   - **Daily / Experimental** — pick from the last 10 dev builds (downloaded from
     the GTNH GitHub Actions artifacts via [nightly.link](https://nightly.link/);
     set a `GITHUB_TOKEN` env var to use the official API and lift rate limits).
   - …or use a `.zip` you've already dropped in this folder.
4. Review exactly what will be **replaced** vs **preserved**, type `YES`, and it
   runs: snapshot → install pack → restore your files → apply config patches →
   copy Additional Files → verify. If anything fails (or you hit Ctrl+C mid-way),
   it **rolls back** automatically.

**Preserved across updates:** worlds (`saves/`), JourneyMap waypoints & maps,
visual prospecting/ore veins, schematics, screenshots, shader/resource packs,
OpenComputers data, `options*.txt`, `servers.dat`, NEI settings, vending favourites,
Botania bookmarks, and more.

### [2] Apply patches & files to instance
Re-apply your **config patches** and/or **Additional Files** to an existing
instance **without** a full pack download/update. Handy after editing a patch or
dropping in a new mod.

### [3] Edit config patches
An in-app editor (no config-file browser) for:

- **Key patches** — change a single setting inside the new pack's config after
  every update (e.g. `B:EnablePollution=false` in `config/GregTech.cfg`). Works
  on Forge `.cfg`, `.properties`, `.json`, and `key: value` files, and preserves
  the file's existing line endings.
- **Presets** — toggleable bundles of tweaks, applied alongside your patches:
  - Disable GT Explosions
  - Remove Pollution
  - Restore Mobspawner Hardness
  - Disable Blood Moon
  - **Adjust ServerUtilities Settings** — set Claim / Chunkloader / Home limits
    (also enables ranks so they apply in singleplayer)
  - **ServerUtilities Commands** — turn individual commands (e.g. `/home`,
    `/back`, `/spawn`) on/off

## Additional Files

Drop personal mods/configs/packs in the **`Additional Files/`** folder and they're
copied into `.minecraft` after every update, structure preserved. Version-aware
conflict handling warns you before downgrading an installed mod. See
[`Additional Files/README.md`](Additional%20Files/README.md).

## Configuration

A few constants at the top of `update_gtnh.py`:

| Setting | Purpose |
|---|---|
| `INSTANCES_ROOT` | Where your launcher keeps instances (defaults to Prism's `%APPDATA%` path). |
| `FIXED_INSTANCE_DIR` | Set a full instance path to skip the picker entirely. |
| `GITHUB_TOKEN` (env var) | Optional — for daily/experimental downloads via the official GitHub API and a higher rate limit. |

Your tweaks live in **`config_patches.json`**, created on first run. It's
git-ignored, so your personal settings stay local. Manage it from the in-app
editor (recommended) or hand-edit it.

## Other platforms

The updater targets **Windows + Prism**. The core (extract, preserve, patch,
backup, rollback) is cross-platform, but instance auto-detection uses the Windows
`%APPDATA%` path. On Linux/macOS, set `FIXED_INSTANCE_DIR` to your instance folder
(the one containing `.minecraft`).

## Disclaimer

Not affiliated with the GT: New Horizons team. Use at your own risk; back up your
instances. Daily/Experimental builds are fetched from public GitHub artifacts via
nightly.link — a third-party mirror.

## License

[MIT](LICENSE) © Willshaper
