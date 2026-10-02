# Diablo 3 Coach

A local, screenshot-driven Diablo III assistant. The current proof of concept
extracts items, visible player/follower stats and materials from **German game
screenshots at 2560 × 1440**, then updates a local JSON character state.

Application messages, documentation and code comments are English. Recognized
game names, affixes and stat labels stay in German. Other game languages and
resolutions are planned, not supported yet.

The default character profile is **Crusader / Aegis of Valor**, with the Season
Journey first and the Altar second. You can change it. Demon Hunter and other
class screenshots used during development are only test data, not strategy
settings. Automatic upgrade scoring and strategic recommendations are not
implemented yet; equipment recommendations currently require user selection.

## Quick start (Windows)

Git is assumed to be installed; no separate Git installation guide is needed.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Ollama for Windows](https://docs.ollama.com/windows), then open a new PowerShell
window so their commands are available. Python 3.12 is required; uv can manage
the interpreter and keeps dependencies in the project's isolated `.venv`.
See [uv project environments](https://docs.astral.sh/uv/guides/projects/).

Clone this repository using its URL from GitHub's **Code** button (replace
`REPOSITORY_URL` below), or download and unpack its source:

```powershell
git clone REPOSITORY_URL diablo3-coach
cd diablo3-coach
uv sync --locked --python 3.12
ollama pull qwen3-vl:8b-instruct
ollama list
Invoke-RestMethod http://localhost:11434/api/tags
uv run python -m unittest -q
```

Ollama must be **running**, not merely installed, and the model must be downloaded.
The API check should return the local model list. If the connection fails, start
Ollama and retry; if the model is missing, run the pull command again.

Image processing uses the local Ollama API at `localhost:11434`; no cloud model
or external screenshot upload is used. Initial tool installation and model
downloads need internet access. Development was tested with an RTX 4070 with
12 GB VRAM while Diablo III was running; this is a tested setup, not a minimum
hardware guarantee. Model inference still competes with the game for GPU
resources, even with a serial queue.

### Process screenshots automatically

Double-click `start-coach.cmd` in the project directory. It uses the existing
`.venv`, needs no administrator rights and prints the absolute input path:

```text
<project directory>\screenshots\incoming\
```

Set that folder as Greenshot's screenshot destination, using unique timestamped
PNG filenames. Leave the starter window open while playing. **Ctrl+C** stops
the watcher. Alternatively, run:

```powershell
uv run python d3coach.py watch
```

Hover over equipped items to record equipment; hover over inventory or stash
items to record candidates. Equipped comparison tooltips may stay visible.
When the full-image layout pass succeeds, both tooltips are processed separately.
The final half-image fallback accepts candidates only.

Player details, follower details and materials are routed automatically at the
tested resolution. Additional screenshots may arrive during processing: the
watcher processes them serially in filename order, waits briefly for unchanged
file size/modification time, and resumes files left in `processing/` first.

The console reports `OK`, `REVIEW` or `ERROR`. Screenshots are moved to:

- `screenshots/done/`: usable data extracted and state updated.
- `screenshots/review/`: no usable data; state unchanged, result JSON retained.
- `screenshots/failed/`: processing failed; screenshot and error text retained.
- `screenshots/processing/`: current work, resumed on the next start.

Recognition JSON is saved atomically in `data/results/`; character state is in
`data/state.json`. Inspect the JSON against the screenshot: `OK` does not mean
every name or number is correct.

Only one watcher can use a queue. After a hard crash, a stale
`screenshots/watcher.lock` may remain. Remove it only after confirming no watcher
is still running.

### Analyze an existing screenshot

Run commands from the project directory:

```powershell
uv run python d3coach.py analyze "C:\path\to\screenshot.png"
```

`analyze` updates the state and prints recognition JSON to stdout. Unlike
`watch`, it does **not** automatically save a result file in `data/results/`.

If automatic routing fails, specify one of the calibrated views:

```powershell
uv run python d3coach.py analyze "materials.png" --screen-type materials
uv run python d3coach.py analyze "player-details.png" --screen-type player_stats
uv run python d3coach.py analyze "follower-details.png" --screen-type follower_stats
uv run python d3coach.py analyze "follower-item.png" --screen-type follower_item
```

These guided views require 2560 × 1440 screenshots. Two overlapping scroll
screenshots of the same stats/materials list can be merged in one serial call:

```powershell
uv run python d3coach.py analyze "top.png" "bottom.png" --screen-type materials
```

Exactly matching row names are merged; the later screenshot's value wins.
Unseen rows are retained, not deleted; quantities are replaced, not added.
The watcher updates the same lists incrementally, so ingest screenshots in
chronological order. `source_images` records the input filenames.

Use `--model <ollama-name>` on `analyze` or `watch` to try another installed
local model. Run `uv run python d3coach.py --help` for the CLI entry points.

## Technical approach: the current harness

The implementation deliberately starts with state and validation, not an
autonomous agent framework:

1. **Queue:** screenshots enter through a file-based input folder.
2. **View routing:** at the reference resolution, deterministic checks detect
   red window frames at calibrated positions. Qwen reads only small title
   crops. Known titles route player/follower details and materials to fixed
   list crops; a follower portrait alone does not identify an open follower view.
3. **Layout:** otherwise, Qwen3-VL locates text panels in the full image and
   returns roles, owners and normalized `box_1000` coordinates. Only if it
   returns no panels, a center crop is tried, followed serially by overlapping
   left/right halves. The first successful fallback stops retries. Half crops
   keep complete item candidates only, not equipped references; panels touching
   an inner crop edge are rejected. Full-image processing remains first so
   future centered views are not forced into halves.
4. **Extraction:** Pillow crops each panel with a small margin. The same local
   vision model extracts item data or visible list rows into a fixed schema.
   Known roles are supplied, rather than guessed again.
5. **Validation:** Pydantic rejects invalid JSON types and unexpected fields.
   Deterministic normalization separates quality (`legendary`, `ancient`,
   `primal`) from independent set membership (`is_set`). Standalone gems use
   `kind=gem`; empty sockets and inserted ordinary/legendary gem effects use
   `socket_effects`, preserving repeated effects.
6. **Persistence:** validated observations update local state. The watcher also
   saves result JSON and archives the input screenshot for review.

Ollama calls use temperature 0 and schema-constrained JSON. Localization and
extraction keep the model loaded within one job; it is requested to unload
afterward, including on errors. One watcher issues one inference call at a
time. Avoid running separate `analyze` jobs alongside it if you want to maintain
that GPU concurrency limit.

This harness consists of strict schemas, a serial workflow, deterministic
postprocessing, state persistence and repeatable offline tests around a
probabilistic model. Cropping reduces irrelevant visual context; it does not
guarantee accurate extraction, and extra fallback calls can increase latency.

### Known limits and experiments

- Model-reported `confidence` describes panel selection, not the reliability of
  every extracted value. Guided selection uses confidence 1 by construction.
- Narrow lists and small titles depend on calibrated window positions.
  Full-image item localization may still work at other sizes, but that does not
  establish supported routing or extraction at those resolutions.
- A short local comparison with `qwen2.5vl:7b` did not produce a more robust
  overall pipeline: some names improved, but it also mixed comparison values
  into affixes and returned pixel coordinates or invented full-image panels.
  Qwen3-VL remains the default.
- Contrast × 1.3 and autocontrast did not fix the missed Buriza/Yang comparison
  in the local trial; the center-crop retry did. Half-image retries also failed
  on some visible tooltips, so they are a fallback, not a guarantee.
- Stash/inventory candidate location uses observed left/right tooltip geometry
  when both window titles are detected. Without inventory context it remains
  unknown. A tooltip does not prove a complete inventory or stash census.
- Cube extraction diamonds are not interpreted yet. Cube powers, Altar,
  skills and Rift results have no dedicated recognition path.
- Direct Python screenshot capture, phone notifications and automatic strategy
  research are not included yet.

## Local character state

`analyze` and `watch` update `data/state.json`; `--state <path>` selects another
file. It stores class, build, season, ordered goals, Season Journey/Altar
progress, player/follower stats, materials, resources and item records with IDs,
locations, sources, souvenir protection and cube status.

The default profile is Crusader / Aegis of Valor; the season is not guessed.
Existing files are **not** translated or reset by English application defaults.
You can keep profile values in your preferred language.

```powershell
uv run python d3coach.py state show
uv run python d3coach.py state configure --class "Crusader" --build "Aegis of Valor" --season "My season" --goal "Complete the Season Journey" --goal "Upgrade the Altar"
uv run python d3coach.py state progress season_journey "Chapter I" done
uv run python d3coach.py state progress altar "Next node" todo
uv run python d3coach.py state resource gold 100000
uv run python d3coach.py state resource blood_shards 500
```

Season Journey, Altar, gold and blood shards are currently maintained manually.
Progress accepts `unknown`, `todo` or `done`; unknown is never treated as
completed. `--goal` replaces the ordered goal list. For a different state file,
place `--file <path>` immediately after `state`, before its subcommand.

### Item identity and corrections

Read IDs using `state show`. You can import watcher results without another GPU
call and explicitly record a candidate's location:

```powershell
uv run python d3coach.py analyze "drop.png" --location stash
uv run python d3coach.py state ingest "data/results/drop.json" --location inventory
uv run python d3coach.py state item ITEM_ID --location stash --souvenir
```

A new candidate screenshot can create a duplicate record of the same real item:
identical names and rolls do not prove identity because multiple copies can
exist. For a known item, especially after a reroll or to correct an assumed
equipment change, provide its ID:

```powershell
uv run python d3coach.py analyze "after-reroll.png" --item-id ITEM_ID
uv run python d3coach.py state ingest "corrected-result.json" --item-id ITEM_ID
```

This requires one candidate with a matching name, or one single item; an equipped
reference may remain visible. Replaying the same recognition, sources and
assignment is idempotent. Empty recognition never deletes existing equipment.

Observed equipped items update unambiguous slots for the same owner. Player and
follower stay separate. Rings and weapons are not automatically displaced:
multiple slots/combinations need explicit replacement. Switching between
different weapon slot types is not implemented.

### Equipment recommendations and the replaced item

```powershell
uv run python d3coach.py state equip NEW_ITEM_ID
uv run python d3coach.py state equip NEW_RING_ID --replace-id OLD_RING_ID
uv run python d3coach.py state item ITEM_ID --cube-status needed
uv run python d3coach.py state item ITEM_ID --cube-status not_needed
```

`state equip` is a **user-selected recommendation**, not a calculated upgrade.
It records the new equipment immediately with `evidence=assumed` and also
returns an action for the replaced item. The old item is assumed to be in
inventory; a salvage/cube recommendation does not claim that disposal happened.

A new screenshot can correct the assumption (`evidence=observed`). An item
displaced by an observation receives location `unknown`, not an invented stash
position. The item `role` records its observation/recommendation context;
`location` represents its current stored placement.

Primal Ancients are permanently protected as souvenirs. Other items can be
protected with `--souvenir`. Replacement disposal follows these safety rules:

- Souvenir or primal: keep.
- Cube power still needed: keep for extraction.
- Cube status unknown: keep and check.
- Explicitly checked as `not_needed`, and not a souvenir: salvage may be advised.

Until strategy rules exist, mark other strategic keepers as souvenirs. The
diamond marker is not read automatically, so cube status defaults to unknown.

### Persistence and recovery

Writes use atomic replacement and an exclusive lock. Invalid state JSON is not
silently reset. After a hard crash, a `.json.lock` may remain; remove it only
when no state writer is running.

If a watcher state update fails, result JSON and the screenshot remain available
(the screenshot goes to `failed/`). Repair the state and import the saved result
with `state ingest`. Back up your state before manual edits.

## Roadmap

Near-term priorities:

- Establish a Crusader baseline and add explainable, goal-driven rules for
  upgrades, rerolls, cube extraction and disposal of replaced items.
- Improve extraction checks and add repeatable local model comparisons.
- Add validated game-language and resolution profiles, contributed by users.

Later increments:

- Direct screenshot capture and convenient phone notifications.
- On-demand strategy research with locally persisted, attributable rules.
- Recognition of Season Journey, Altar, extracted cube powers and skills.
- Rift / Greater Rift result ingestion and broader next-action planning.

These are directions, not implemented features or release-date commitments.

## Development and contributions

Use test-driven development where behavior changes: add a failing test, make it
pass, then refactor. **All tests must be green before the next increment/commit.**

```powershell
uv run python -m unittest -v
```

Tests generate small synthetic images in temporary directories and mock Ollama.
They need neither a GPU/running model nor ignored/private screenshot folders.
Real game screenshots are optional local evaluation inputs; compare extracted
JSON with the source image, especially names, sockets, powers and numbers.
Offline tests do not measure real-model recognition accuracy.

Contributor entry points:

- [game_de.py](game_de.py) contains German recognition prompts, title vocabulary,
  slot labels and normalization markers. Application UI is English. A new
  language needs corresponding labels, normalization cases and local screenshot
  checks, not just translated prompts. Runtime language selection and canonical
  cross-language stat/item identifiers remain roadmap work.
- [screen_layout.py](screen_layout.py) contains reference x/y resolution, pixel
  header/frame boxes, normalized guided crops, and named half-screen/overlap
  factors. `BOX_SCALE=1000` is the model's coordinate protocol, not the screen
  resolution. Header boxes are reference pixels; guided/fallback boxes are
  normalized coordinates. Do not simply scale reference pixel boxes and claim
  another resolution is supported: validate UI scaling, aspect ratio, tooltip
  edges and all supported views first.
- [d3coach.py](d3coach.py) contains validation, normalization, state and CLI
  workflow. Keep additions small and supply offline regression tests. For
  recognition changes, report the language, resolution, model, expected result
  and actual result from a local evaluation.

The language/layout values are separated now so contributors have clear places
to work, without introducing an unused translation framework or profile loader.
Currently only the German vocabulary and one calibrated resolution are wired in.

Do not commit personal state, secrets or game screenshots. `.idea/`, `*.iml`,
`.venv/`, `screenshots/` and `data/` are ignored. The public test suite needs
no large binaries or Git LFS. If sharing evaluation images becomes necessary,
agree on distribution rights and storage separately; the code license does not
grant rights to Blizzard assets.

## License and disclaimer

The source code is available under the [MIT License](LICENSE), permitting use,
modification and redistribution with the copyright and license notice retained.

Diablo and Diablo III are trademarks of Blizzard Entertainment. This unofficial
fan project is not affiliated with or endorsed by Blizzard Entertainment.
