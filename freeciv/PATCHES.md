# XBWorld Branch — Custom Patches

This document lists all custom commits on the `xbworld` branch of
[xingbo778/freeciv](https://github.com/xingbo778/freeciv), applied on top
of upstream `freeciv/freeciv` at commit `add9f4e14e`.

## Fork Point

- **Upstream commit**: `add9f4e14ebf8609f369d586e42cd2ccca2bc6df`
- **Upstream branch**: `main`
- **Description**: "Comment typofix: splitted -> split"

## Custom Commits (oldest → newest)

### 1. Protocol & Compatibility

| Commit | Description |
|--------|-------------|
| `87dfc8c62b` | **feat: set web capstring** — Replaces the native network capability string with a web-compatible one for freeciv-web protocol compatibility. |

### 2. Upstream Backports (Bug Fixes)

These fix specific bugs reported in the Freeciv tracker:

| Commit | Patch | Tracker |
|--------|-------|---------|
| `8396f811b4` | Fix combat veterancy chance | [RM #983](https://redmine.freeciv.org/issues/983) |
| `23c5d8721f` | Make action selection dialog appear on airlift | [RM #1028](https://redmine.freeciv.org/issues/1028) |
| `287812a8b6` | Send unit info only if server-side agent set | [RM #1104](https://redmine.freeciv.org/issues/1104) |

### 3. Freeciv-Web Patches

Patches ported from the [freeciv-web](https://github.com/freeciv/freeciv-web)
project to make the server work with web clients:

| Commit | Patch Name | Purpose |
|--------|------------|---------|
| `3e153f4d33` | RevertAmplio2ExtraUnits | Revert breaking changes from amplio2 extra_units.spec |
| `9402537359` | meson_webperimental | Install webperimental ruleset via meson |
| `c79cb26281` | metachange | Metaserver integration changes |
| `6d52c4cea3` | text_fixes | Text and translation fixes |
| `68469f2af0` | freeciv-svn-webclient-changes | Core server changes for web client support |
| `03147b7c38` | goto_fcweb | Goto/pathfinding adjustments for web |
| `d2b9cd9371` | savegame | Savegame format changes for web compatibility |
| `0004eb19c5` | maphand_ch | Map handling changes for web client |
| `315287af6f` | server_password | Server password authentication support |
| `2e772c944b` | scorelog_filenames | Custom scorelog filename handling |
| `9b7eaddf2a` | longturn | Basic longturn mode for Freeciv-web |
| `bb3cdb6bc2` | load_command_confirmation | Log message confirming load completion (used by Freeciv-web to issue follow-up commands) |
| `555dddafb0` | webgl_vision_cheat_temporary | Temporary: reveal terrain types to WebGL client |
| `0ecd075907` | endgame-mapimg | Generate map image at endgame for hall of fame |
| `b3849c5fea` | stdsounds_format | Standard sounds format compatibility |

### 4. XBWorld Custom

| Commit | Description |
|--------|-------------|
| `9364fd8f43` | **feat: add xbworld custom ruleset** — Adds the `data/xbworld/` ruleset directory based on webperimental, customized for AI-agent games. Includes all `.ruleset` files, `script.lua`, and `README.xbworld`. |

### 5. Cleanup (this optimization pass)

| Commit | Description |
|--------|-------------|
| *(latest)* | **chore: remove .orig backup files** — Removes 8 `.orig` files (35K lines) left over from the patch workflow. |
| *(latest)* | **fix: update XBWorld ruleset naming and Lua safety** — Renames "Webperimental" → "XBWorld", fixes typo, standardizes descriptions, adds zero-guards in `script.lua`. |

### 6. Performance Optimizations (XBWorld)

| Commit | Description |
|--------|-------------|
| `5113b1e9ef` | **perf: early-exit O(n²) diplomatic intel loop in daidata.c** — Adds NULL guards and a `break` to the inner `players_iterate` in `dai_data_phase_begin()`, converting the worst-case O(n³) scan (outer player × aplayer × check_pl) to stop as soon as all three `ai_dip_intel` pointer fields are found. Behavior is equivalent: first matching player is stored instead of last, which is equally valid for both boolean and `player_name()` uses in `daidiplomacy.c`. |
| `1a7004a2ae` | **perf: replace select() with epoll on Linux in sernet.c** — On Linux, `server_sniff_all_input()` now uses `epoll_wait()` instead of `fc_select()`. Epoll fd lifecycle: created in `server_open_socket()`, fds added in `server_make_connection()`, removed in `close_connection()`. EPOLLOUT toggled per-connection before each wait; events synthesized into the existing `readfs`/`writefs`/`exceptfs` fd_sets for unchanged dispatch code. Full select() fallback preserved for non-Linux builds. Eliminates O(max_fd) fd_set rebuild on every loop iteration; `epoll_wait()` is O(ready fds). |
| `161af5e26c` | **perf: pre-compute shared-enemy counts in dai_diplomacy_begin_new_phase()** — Builds a `shared_enemy_count[player_slot_count()]` array in one O(P²) pass before the per-aplayer love loop, replacing a previously-nested O(P) players_iterate_alive inside the love-adjustment block. |
| `8cbceeef68` | **perf: pre-compute units_in_our_territory in dai_diplomacy_begin_new_phase()** — Builds a `units_in_our_territory[player_slot_count()]` array before the per-aplayer love loop, replacing `player_in_territory(pplayer, aplayer)` which iterated all of aplayer's units per player iteration. |
| `578aacc218` | **perf: hoist pplayer self-iteration out of dai_war_desire()** — Pre-computes `pp_fear`, `pp_want`, `pp_settlers`, `pp_cities` from pplayer's own units and cities once before the per-aplayer call site, eliminating duplicate O(U+C) work that was repeated inside `dai_war_desire()` for each target player. |
| `e058bd29a8` | **perf: O(T²)→O(T+\|pos\|×\|neg\|) in suggest_tech_exchange()** — Replaces double `advance_index_iterate_max` (T² iterations) with a pre-filter pass building `pos_techs[]` and `neg_techs[]` lists, then matching only the cross-product of those two small arrays. In practice ~25 iterations vs ~6400 for an 80-tech game. |
| `f63ded7c65` | **perf: O(P²)→O(P) in dai_diplomacy_actions DS_ALLIANCE branch** — Pre-builds `pp_enemies[]` (pplayer's war targets) before the outer `players_iterate_alive` loop; the inner `players_iterate_alive` in the DS_ALLIANCE `switch` case is replaced by a loop over this small list (0–3 entries typical). |
| `91081e5a7b` | **perf: hoist team-nplayers computation out of per-city×improvement loop in daicity.c** — `adjust_improvement_wants_by_effects()` contained a `players_iterate` to count pplayer's team members and adjust nplayers. Result is identical for all cities and improvements of the same pplayer. Pre-computed once in `dai_build_adv_adjust()` and passed as a parameter, reducing O(I×C×P) → O(P + I×C). |
| `e59af74de5` | **perf: hoist obsolescence-turns from per-city to per-improvement in daicity.c** — The `players_iterate` that computes minimum turns until any player researches the obsolescence tech only depends on `pimprove`, not on `pcity`. Moved from inside `adjust_improvement_wants_by_effects()` to the improvement level in `dai_build_adv_adjust()`, reducing O(I×C×P) → O(I×P) per AI player per turn. |
| `88b8e89719` | **perf: O(P²)→O(P) in adv_data_phase_init allied_with_enemy loop** — Pre-builds `adv_enemies[]` (pplayer's war enemies) before the outer aplayer loop; the inner `players_iterate(check_pl)` is replaced by a scan over this short list plus early `break`, eliminating the O(P²) pattern in the advisor diplomacy init. |
| `044428e231` | **perf: O(P²)→O(P) in dai_data_phase_begin ai_dip_intel loop** — Pre-builds `ddat_enemies[]` and `ddat_allies[]` for pplayer before the outer aplayer loop; three inner field assignments replaced by targeted scans over these small lists (0–3 entries typical), reducing total work from O(P²) to O(P) per pplayer per turn. |
| `5f8d496b88` | **perf: hoist has_handicap() out of EFT_GAIN_AI_LOVE players_iterate loop in daieffects.c** — `has_handicap(pplayer, H_DEFENSIVE)` is pplayer-constant; calling it once per AI player wasted O(P) calls per `dai_effect_value()` invocation. Pre-compute `per_ai` once, count `n_ai` in one pass, then `v += n_ai * per_ai`. |
| `4e5c04a5d2` | **perf: merge production_leader and tech_leader scans into one O(P) pass in advdata.c** — `adv_data_phase_init()` ran two consecutive `players_iterate` loops to find the production leader (max score.mfg) and tech leader (max score.techs). Merged into one loop, halving the per-player iteration count for these two max-finding scans. |
| `c0a721f7a6` | **perf: pre-build hostile player list in find_something_to_kill()** — `find_something_to_kill()` (called per attacking unit per turn) contained two `players_iterate` loops each gated by `POTENTIALLY_HOSTILE_PLAYER` / `pplayers_at_war`. Pre-builds `fstk_hostile[]` in one O(P) pass; both loops then iterate only 1–3 hostile players instead of all P players, eliminating O(P_neutral × 2) filter evaluations per call. |
| `f34644c27d` | **perf: reorder and merge diplstate lookups in dai_war_desire() treaty loop** — Inner `players_iterate_alive` in `dai_war_desire()` (called O(P) times per player per turn) previously called `player_diplstate_get(pplayer, eplayer)` twice before checking `pplayers_allied(target, eplayer)`. Reordered to check allied status first (fast reject for non-allied majority), then fetch diplstate once only for allied players. Saves 2 `player_diplstate_get()` calls per non-allied player per call. |
| `288e1a4f28` | **perf: hoist adv_is_player_dangerous() out of per-city assess_danger() loop in daimilitary.c** — `dai_assess_danger_player()` called `assess_danger()` once per city; inside, a `players_iterate` called `adv_is_player_dangerous(pplayer, aplayer)` for every player — O(C×P) total calls. Since the result is city-independent, pre-builds the dangerous-player list once in O(P) and passes it to all city calls, reducing total calls from O(C×P) to O(P). assess_danger() inner loop replaced with O(D) for-loop (D = 1-3 dangerous players typical). |
| `315e98b5be` | **perf: cache n_ai/new_contacts/parasite_bulbs in adv_data.stats** — Three effects in `dai_effect_value()` each ran a full `players_iterate` per call: `EFT_GAIN_AI_LOVE` (count AI players), `EFT_HAVE_CONTACTS` (count expired-contact players), `EFT_TECH_PARASITE` (sum bulbs from non-team players). Since `dai_effect_value()` is called O(C×I) times per AI turn, these produced O(C×I×P) total iterations. Adds `n_ai`, `new_contacts`, `parasite_bulbs` to `adv_data.stats`, computed once per player per phase in a single `players_iterate_alive` pass. The three effect cases now read O(1) from `adv->stats`. |
| `f419f1c5c5` | **perf: cache nplayers in adv_data.stats, fold into existing O(P) pass** — `dai_build_adv_adjust()` and `dai_tech_effect_values()` each ran an identical O(P) `players_iterate` to compute `nplayers = normal_player_count() - same_team_members`. Adds `adv_data.stats.nplayers` computed in `adv_data_phase_init()` by folding the team-subtraction into the existing `players_iterate` that builds `adv_enemies[]`. Three loops → one; both callers replaced with `adv->stats.nplayers`. |
| `fedc97229e` | **perf: merge enemies/nplayers/leaders into one O(P) pass in advdata.c** — `adv_data_phase_init()` ran three consecutive `players_iterate` loops: (1) build `adv_enemies[]` + compute `stats.nplayers`, (2) set `allied_with_enemy` (kept separate — needs enemies[] complete), (3) find `production_leader` + `tech_leader`. Loops 1 and 3 are independent; merged into one pass. Three `players_iterate` → two per AI player per phase. |
| `ca30630ff1` | **perf: hoist dai_on_war_footing() out of per-city loops in daicity.c** — `dai_manage_cities()` called `dai_on_war_footing()` (O(P)) once per city in two separate `city_list_iterate` loops (directly at line 934, and inside `dai_city_choose_build()` at line 275), totalling O(2×C×P) per player per turn. Added `bool war_footing` parameter to `dai_city_choose_build()`; compute result once before both loops. O(2×C×P) → O(P + 2×C). |
| `9709b35278` | **perf: pre-build danger[] in dai_hunter_manage() to avoid per-unit adv_is_player_dangerous()** — `adv_is_player_dangerous()` (virtual-dispatch + diplstate lookups) was called once per target unit inside `pf_map_move_costs_iterate × unit_list_iterate_safe` — O(T×U) calls per hunter unit per turn. Pre-builds `bool danger[player_slot_count()]` in one O(P) `players_iterate` pass before the pathfinding loop. Inner check becomes O(1) array lookup. |
| `c3e62d14a5` | **perf: merge revenge-war and target-find loops in dai_diplomacy_actions()** — Two consecutive `players_iterate_alive` passes — revenge-war countdown (line 1696) and most-hated target scan (line 1764) — iterate the same player set. Target-finding reads only `WAR()` and `love[]`, neither modified by `war_countdown()`, so merging is safe. One `players_iterate_alive` eliminated per AI player per turn. |
| `1a74bc296b` | **perf: hoist dangerous-player build out of military_advisor_choose_build()** — `military_advisor_choose_build()` (called once per city per turn) rebuilt the dangerous-player list via `adv_is_player_dangerous()×P` per call — O(C×P) per AI player. Since the list is pplayer-constant, moved the `players_iterate` scan to callers (`dai_manage_cities()` and tex AI city loop) and pass the list in. O(C×P) → O(P + C). Updated `daimilitary.h` signature. |
| `4ada8de62c` | **perf: merge love-increment and love-cooling into one loop in dai_diplomacy_begin_new_phase()** — Two consecutive `players_iterate` passes at the end of the function (love adjustments + love-coeff decay/clamp) combined into one `players_iterate_alive`. Pre-computes `love_coeff/100.0` once per call (removes per-player FP division). Saves one full O(P) pass per AI player per turn. |
| `72f6065962` | **perf: merge units_in_territory+war_desire loops and countdown+pp_enemies loops in daidiplomacy.c** — (1) In `dai_diplomacy_begin_new_phase()`: the `units_in_our_territory` pre-build pass and the `war_desire` computation pass are independent (war_desire doesn't read units_in_our_territory); merged into one `players_iterate_alive`, saving one O(P) pass per AI player per turn. (2) In `dai_diplomacy_actions()`: the countdown-decrement loop (`players_iterate`) and the `pp_enemies` pre-build (`players_iterate_alive`) merged into one `players_iterate`; pp_enemies populated after any `dai_go_to_war()` calls so newly-declared wars are captured. Saves one O(P_alive) pass per AI player per turn. |
| `9057764695` | **perf: merge n_ai/contacts/parasite stats pass into adv_enemies loop in advdata.c** — `adv_data_phase_init()` ran two consecutive O(P) passes: (1) `players_iterate_alive` for `n_ai`, `new_contacts`, `parasite_bulbs`; (2) `players_iterate` for `adv_enemies[]`, `nplayers`, production/tech leaders. The alive-only stats are independent of the enemies/leaders data; merged into one `players_iterate` with an `is_alive` guard for the stats fields. Saves one O(P_alive) pass per AI player per phase. |
| `a2bd92fd34` | **perf: merge winner-check + candidate-count loops in check_for_game_over()** — `check_for_game_over()` (called every turn) ran two consecutive O(P) `players_iterate` passes: (1) scan for PSTATUS_WINNER / EFT_VICTORY (scenario victory); (2) count alive candidates and defeated players. The two passes are independent; merged into one `players_iterate` that performs both checks simultaneously. `candidates`/`defeated` are stack variables; the minor extra arithmetic in the rare winner branch is negligible vs. saving a full O(P) traversal each turn. |

## Known Issues in C Code

These are pre-existing FIXMEs/TODOs in the upstream code that are
particularly relevant to XBWorld:

| File | Line | Issue | Impact |
|------|------|-------|--------|
| `server/scripting/api_server_game_methods.c` | 97 | Client unaware when player killed by Lua script | **High** — affects AI-agent games |
| `server/srv_main.c` | 2636 | Web client: connection username == player name assumption | **High** — may cause issues with multiple connections |
| `server/srv_main.c` | 2993 | HACK: skip wait during AI phases | **Medium** — hardcoded behavior |
| `server/diplomats.c` | 1528 | Lua script may have destroyed diplomat | **Medium** — potential null dereference |
| `server/cityturn.c` | 2496 | Duplicate of `can_upgrade_unittype` | **Low** — code duplication |
| `server/srv_main.h` | 44 | `load_filename[512]` may be too short | **Low** — potential buffer issue |

These are tracked for future work but not modified in this pass to
minimize risk of breaking upstream compatibility.
