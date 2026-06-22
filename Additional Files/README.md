# Additional Files

Anything you put in this folder is copied into your instance's `.minecraft`
**after every update** (and via *Apply patches & files*), preserving the folder
structure exactly. Use it for personal mods, configs, resource packs, or shaders
that you always want kept across pack updates.

Example layout:

```
Additional Files/
├── mods/          → BetterFoliage-MC1.7.10-2.0.17.jar
├── config/        → BetterFoliage.cfg
├── resourcepacks/ → MyTexturePack.zip
└── shaderpacks/   → Sildurs.zip
```

When a mod here clashes with one already installed (same mod, different version),
the updater compares versions, warns you about downgrades, and lets you skip the
older file or disable the installed one.

> The contents of this folder are git-ignored — your personal files stay local.
> Only this README is tracked so the folder exists in the repo.
