Freeciv C Server (XBWorld Fork)
================================

This directory contains the Freeciv C game server, customized for XBWorld
AI-agent games. The server source is managed as a **git submodule** pointing
to [xingbo778/freeciv](https://github.com/xingbo778/freeciv) (branch `xbworld`).

## Directory Layout

```
freeciv/
├── freeciv/              ← Git submodule (Freeciv C source)
│   ├── server/           ← Game server core
│   ├── common/           ← Shared game logic
│   ├── ai/               ← AI implementations
│   ├── data/xbworld/     ← XBWorld custom ruleset
│   └── ...
├── prepare_freeciv.sh    ← Build script (configure + compile)
├── freeciv-web.fcproj    ← Project-specific server config
├── version.txt           ← Records the upstream commit we forked from
├── PATCHES.md            ← Documents all custom patches on xbworld branch
└── README.md             ← This file
```

## Quick Start

```bash
# Build (first run initializes the submodule automatically)
./prepare_freeciv.sh

# Build with -Dwerror=true for CI
./prepare_freeciv.sh TEST

# Clean rebuild
./prepare_freeciv.sh clean
```

The compiled server is installed to `~/freeciv/bin/freeciv-web`.

## How Patches Work

All patches from the original freeciv-web project have been committed as
individual git commits on the `xbworld` branch of the fork. There is no
download-and-patch step — the submodule already contains the patched source.

See [PATCHES.md](PATCHES.md) for a full list of custom commits and their
purpose.

## Syncing with Upstream Freeciv

The fork (`xingbo778/freeciv`) was created from upstream
[freeciv/freeciv](https://github.com/freeciv/freeciv) at commit
`add9f4e14e` (the `main` branch). The `xbworld` branch has 20+ custom
commits on top.

To sync with upstream:

```bash
cd freeciv                          # enter submodule
git remote add upstream https://github.com/freeciv/freeciv.git  # one-time
git fetch upstream
git rebase upstream/main            # or merge, depending on preference
# Resolve conflicts, then push
git push origin xbworld
```

> **Note**: The upstream `main` branch is currently ~1100 commits ahead.
> A rebase should be done carefully, testing each patch for conflicts.

## XBWorld Ruleset

The custom ruleset lives in `freeciv/data/xbworld/`. Key files:

| File | Purpose |
|------|---------|
| `game.ruleset` | Global game parameters, victory conditions |
| `units.ruleset` | Unit types, stats, flags |
| `techs.ruleset` | Technology tree |
| `buildings.ruleset` | City improvements and wonders |
| `effects.ruleset` | Numeric effects (largest/most complex file) |
| `governments.ruleset` | Government types |
| `actions.ruleset` | Unit action configuration |
| `terrain.ruleset` | Terrain types and output |
| `script.lua` | Lua event handlers |

See `freeciv/data/xbworld/README.xbworld` for inherited rules from
the webperimental ruleset.
