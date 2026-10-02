import argparse
import base64
import json
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Annotated, Literal
from urllib.request import Request, urlopen

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import game_de as game
from game_de import (
    HeaderTitle, ITEM_PROMPT, LAYOUT_PROMPT, MATERIALS_PROMPT, STATS_PROMPT, VIEW_PROMPT,
)
from screen_layout import (
    BOX_SCALE, CROP_EDGE_MARGIN, CROP_PADDING, FALLBACK_BOX, GUIDED_PANELS,
    HALF_BOXES, HEADER_BANDS, HEADER_FRAMES, REFERENCE_SIZE,
    FRAME_RED_MINIMUM, FRAME_RED_DOMINANCE, FRAME_RED_FRACTION,
)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3-vl:8b-instruct"
NonNegativeInt = Annotated[int, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ViewContext(StrictModel):
    screen_type: Literal["items_or_other", "player_stats", "materials", "follower_stats"] = "items_or_other"
    follower_open: bool = False
    stash_open: bool = False
    inventory_open: bool = False


class ViewTitles(StrictModel):
    left_title: HeaderTitle
    middle_left_title: HeaderTitle
    middle_right_title: HeaderTitle
    right_title: HeaderTitle

    def context(self) -> ViewContext:
        left, middle_left, middle_right, right = (
            value.strip().upper() for value in
            (self.left_title, self.middle_left_title, self.middle_right_title, self.right_title))
        follower = left in game.FOLLOWER_TITLES
        screen_type = "items_or_other"
        if middle_right == game.MATERIALS_TITLE:
            screen_type = "materials"
        elif follower and middle_left == game.DETAILS_TITLE:
            screen_type = "follower_stats"
        elif middle_right == game.DETAILS_TITLE:
            screen_type = "player_stats"
        return ViewContext(screen_type=screen_type, follower_open=follower,
                           stash_open=left == game.STASH_TITLE, inventory_open=right == game.INVENTORY_TITLE)


class ViewTitle(StrictModel):
    title: HeaderTitle


class Panel(StrictModel):
    kind: Literal["item", "player_stats", "materials", "follower_stats", "unknown"]
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    title: str
    box_1000: list[int] = Field(min_length=4, max_length=4)

    @field_validator("box_1000")
    @classmethod
    def validate_box(cls, box: list[int]) -> list[int]:
        if any(coordinate < 0 or coordinate > BOX_SCALE for coordinate in box):
            raise ValueError("box coordinates must be between 0 and 1000")
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("box must have positive width and height")
        return box


class Item(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    observed_location: Literal["unknown", "inventory", "stash"] = "unknown"
    name: str
    kind: Literal["equipment", "gem"] = "equipment"
    quality: Literal[
        "normal",
        "magic",
        "rare",
        "legendary",
        "ancient",
        "primal",
        "unknown",
    ]
    is_set: bool
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    socket_effects: list[str] = Field(default_factory=list)
    set_name: str | None
    legendary_power: str | None


class RawItem(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    name: str
    quality_text: str = Field(
        description="Exact colored type line beside the item icon, e.g. 'Uraltes Set: Stiefel'"
    )
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    socket_effects: list[str] = Field(
        default_factory=list,
        description="Each inserted gem effect or 'Leerer Sockel', preserving duplicates",
    )
    set_name: str | None = Field(
        description="Green set heading above the set-piece list; never the item name"
    )
    legendary_power: str | None = Field(
        description="Orange item-specific power only; never a green set-piece list or set bonus"
    )


class VisibleStats(StrictModel):
    values: dict[str, str]


class VisibleMaterials(StrictModel):
    values: dict[str, NonNegativeInt]


class Recognition(StrictModel):
    screen_type: Literal[
        "single_item",
        "item_comparison",
        "player_stats",
        "materials",
        "follower_item",
        "follower_stats",
        "other",
    ]
    panels: list[Panel]
    items: list[Item]
    stats: dict[str, str]
    follower_stats: dict[str, str] = Field(default_factory=dict)
    materials: dict[str, NonNegativeInt]
    confidence: float = Field(ge=0, le=1)
    source_images: list[str] = Field(default_factory=list)


class Layout(StrictModel):
    screen_type: Literal[
        "single_item",
        "item_comparison",
        "player_stats",
        "materials",
        "follower_item",
        "follower_stats",
        "other",
    ]
    panels: list[Panel]
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: int | float) -> float:
        return value / 100 if 1 < value <= 100 else value

    @model_validator(mode="after")
    def deduplicate_panels(self):
        unique: dict[tuple[str, tuple[int, ...]], Panel] = {}
        for panel in self.panels:
            key = (panel.kind, tuple(panel.box_1000))
            if key not in unique or panel.role == "equipped":
                unique[key] = panel
        self.panels = list(unique.values())
        if self.screen_type == "item_comparison" and len(self.panels) == 1:
            self.screen_type = "single_item"
        return self


class StoredItem(StrictModel):
    item: Item
    location: Literal["equipped", "inventory", "stash", "unknown"] = "unknown"
    evidence: Literal["observed", "assumed"] = "observed"
    souvenir: bool = False
    cube_status: Literal["unknown", "needed", "not_needed"] = "unknown"
    source_images: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def protect_primal(self):
        self.souvenir = self.souvenir or self.item.quality == "primal"
        return self


class EquipmentAdvice(StrictModel):
    item_id: str
    action: Literal["equip"] = "equip"
    replaced_item_id: str | None
    replaced_action: Literal["keep", "cube", "salvage"] | None
    reason: str


class CoachState(StrictModel):
    schema_version: Literal[1] = 1
    character_class: str = "Crusader"
    build: str = "Aegis of Valor"
    season: str | None = None
    goals: list[str] = Field(default_factory=lambda: ["Complete the Season Journey", "Upgrade the Altar"])
    season_journey: dict[str, Literal["unknown", "todo", "done"]] = Field(default_factory=dict)
    altar: dict[str, Literal["unknown", "todo", "done"]] = Field(default_factory=dict)
    stats: dict[str, str] = Field(default_factory=dict)
    follower_stats: dict[str, str] = Field(default_factory=dict)
    materials: dict[str, NonNegativeInt] = Field(default_factory=dict)
    resources: dict[str, NonNegativeInt] = Field(default_factory=dict)
    items: dict[str, StoredItem] = Field(default_factory=dict)
    observations: list[str] = Field(default_factory=list)
    recommendations: list[EquipmentAdvice] = Field(default_factory=list)


def load_state(path: Path) -> CoachState:
    return CoachState.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else CoachState()


def save_state(path: Path, state: CoachState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def state_lock(path: Path, *, timeout: float = 5):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = monotonic() + timeout
    while True:
        try:
            handle = lock.open("x")
            break
        except FileExistsError:
            if monotonic() >= deadline:
                raise TimeoutError(f"State is locked: {lock}; after a crash remove this lock only when no writer is running")
            sleep(0.1)
    try:
        yield
    finally:
        handle.close()
        lock.unlink()


def disposal_advice(stored: StoredItem) -> tuple[str, str]:
    if stored.souvenir or stored.item.quality == "primal":
        return "keep", "Souvenir/primal item: keep."
    if stored.cube_status == "needed":
        return "cube", "Keep and extract the still-needed cube power."
    if stored.cube_status == "unknown":
        return "keep", "Cube status unknown: check before salvaging."
    return "salvage", "Cube check completed; not a souvenir."


def recommend_equip(state: CoachState, item_id: str, *, replace_id: str | None = None) -> EquipmentAdvice:
    stored = state.items[item_id]
    item = stored.item
    if item.kind != "equipment" or item.owner == "unknown" or not item.slot:
        raise ValueError("equipping requires equipment with a known owner and slot")
    # ponytail: rings and weapon combinations need explicit replacement, not a guessed slot.
    ambiguous = item.slot in game.AMBIGUOUS_SLOTS
    if ambiguous and replace_id is None:
        raise ValueError("rings/weapons require --replace-id; an empty slot is not assumed")
    existing = [key for key, value in state.items.items() if key != item_id
                and value.location == "equipped" and value.item.owner == item.owner
                and value.item.slot == item.slot]
    if replace_id is None:
        if len(existing) > 1:
            raise ValueError("multiple equipped items require --replace-id")
        replace_id = existing[0] if existing else None
    elif replace_id == item_id or replace_id not in existing:
        raise ValueError("replacement must be an equipped item with the same owner and slot")
    action, reason = disposal_advice(state.items[replace_id]) if replace_id else (None, "No recorded item to replace.")
    advice = EquipmentAdvice(item_id=item_id, replaced_item_id=replace_id,
                             replaced_action=action, reason=reason)
    if replace_id:
        state.items[replace_id].location = "inventory"
        state.items[replace_id].evidence = "assumed"
    stored.location = "equipped"
    stored.evidence = "assumed"
    stored.item = item.model_copy(update={"role": "equipped"})
    state.recommendations.append(advice)
    return advice


def update_state(state: CoachState, result: Recognition, *, location: str = "unknown",
                 item_id: str | None = None) -> None:
    if location not in {"unknown", "inventory", "stash"}:
        raise ValueError("candidate location must be unknown, inventory or stash")
    selected = [index for index, item in enumerate(result.items) if item.role == "candidate"]
    if len(result.items) == 1:
        selected = [0]
    if item_id is not None and (item_id not in state.items or len(selected) != 1):
        raise ValueError("--item-id requires one candidate (or one single item) and an existing ID")
    if item_id and state.items[item_id].item.name.casefold() != result.items[selected[0]].name.casefold():
        raise ValueError("recognized name does not match --item-id")
    observation = sha256((result.model_dump_json() + location + str(item_id)).encode()).hexdigest()
    if observation in state.observations:
        return
    state.stats.update(result.stats)
    state.follower_stats.update(result.follower_stats)
    state.materials.update(result.materials)
    for index, item in enumerate(result.items):
        matches = [key for key, value in state.items.items()
                   if value.item.model_dump(exclude={"role", "observed_location"}) == item.model_dump(exclude={"role", "observed_location"})]
        known_slot = item.owner != "unknown" and item.slot and item.slot not in game.AMBIGUOUS_SLOTS
        key = item_id if item_id and index == selected[0] else None
        if key is None and item.role == "equipped" and known_slot and len(matches) == 1:
            key = matches[0]
        if key is None:
            # ponytail: no fuzzy identity matching; new screenshots can be duplicate observations.
            key = sha256(json.dumps([result.source_images, index, item.model_dump()],
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        previous = state.items.get(key)
        if item.role == "equipped" and known_slot:
            for other_key, other in state.items.items():
                if other_key != key and other.location == "equipped" and other.item.owner == item.owner and other.item.slot == item.slot:
                    other.location = "unknown"
        state.items[key] = StoredItem(
            item=item, location="equipped" if item.role == "equipped" else
            location if location != "unknown" else item.observed_location
            if item.observed_location != "unknown" or previous is None else previous.location,
            souvenir=previous.souvenir if previous else False,
            cube_status=previous.cube_status if previous else "unknown",
            source_images=result.source_images,
        )
    state.observations.append(observation)


def persist_recognition(path: Path, result: Recognition, *, location: str = "unknown",
                        item_id: str | None = None) -> None:
    with state_lock(path):
        state = load_state(path)
        update_state(state, result, location=location, item_id=item_id)
        save_state(path, state)


def locate(
    image: Path,
    *,
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> Layout:
    content = _ask(
        image,
        prompt=LAYOUT_PROMPT,
        schema=Layout.model_json_schema(),
        num_predict=512,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return Layout.model_validate_json(content)


def locate_with_fallback(
    image: Path, *, model: str = MODEL, keep_alive: int | str = 0
) -> Layout:
    layout = locate(image, model=model, keep_alive=keep_alive)
    if layout.panels:
        return layout
    with TemporaryDirectory() as root:
        for index, region in enumerate([FALLBACK_BOX, *HALF_BOXES]):
            crop = crop_panel(image, region, Path(root) / f"retry-{index}.png", padding=0)
            retry = locate(crop, model=model, keep_alive=keep_alive)
            panels = []
            for panel in retry.panels:
                if index and (panel.kind != "item" or panel.role != "candidate"):
                    continue
                box = panel.box_1000.copy()
                if (region[0] > 0 and box[0] <= CROP_EDGE_MARGIN) or (region[2] < BOX_SCALE and box[2] >= BOX_SCALE - CROP_EDGE_MARGIN):
                    continue
                box[0] = region[0] + box[0] * (region[2] - region[0]) // BOX_SCALE
                box[2] = region[0] + box[2] * (region[2] - region[0]) // BOX_SCALE
                panels.append(panel.model_copy(update={"box_1000": box}))
            if panels:
                return Layout(screen_type="single_item" if len(panels) == 1 and
                              panels[0].kind == "item" else retry.screen_type,
                              panels=panels, confidence=retry.confidence)
    return layout


def extract_item(
    image: Path,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> Item:
    content = _ask(
        image,
        prompt=ITEM_PROMPT.format(role=role, owner=owner),
        schema=RawItem.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return normalize_item(
        RawItem.model_validate_json(content), role=role, owner=owner
    )


def extract_stats(
    image: Path,
    *,
    owner: Literal["player", "follower"],
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> dict[str, str]:
    content = _ask(
        image,
        prompt=STATS_PROMPT.format(owner=owner),
        schema=VisibleStats.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return VisibleStats.model_validate_json(content).values


def extract_materials(
    image: Path,
    *,
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> dict[str, int]:
    content = _ask(
        image,
        prompt=MATERIALS_PROMPT,
        schema=VisibleMaterials.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return VisibleMaterials.model_validate_json(content).values


def normalize_item(
    raw: RawItem,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
) -> Item:
    quality_text = raw.quality_text.casefold()
    is_gem = quality_text == game.GEM_LABEL
    # ponytail: observed weapon slots only; add labels when a new screenshot needs one.
    is_weapon = raw.slot is not None and raw.slot.casefold() in game.WEAPON_SLOTS
    name = raw.name
    set_name = raw.set_name or None
    if set_name and name.casefold() == quality_text and game.SET_LABEL not in quality_text:
        name, set_name = set_name, None
    is_set = game.SET_LABEL in quality_text or (not quality_text and set_name is not None)
    if not quality_text and set_name:
        if game.SET_LABEL in set_name.casefold():
            quality_text = set_name.casefold()
            set_name = None
        else:
            quality_text = game.LEGENDARY_LABEL
    if game.PRIMAL_LABEL in quality_text:
        quality = "primal"
    elif game.ANCIENT_LABEL in quality_text:
        quality = "ancient"
    elif game.LEGENDARY_LABEL in quality_text or is_set:
        quality = "legendary"
    elif game.RARE_LABEL in quality_text:
        quality = "rare"
    elif game.MAGIC_LABEL in quality_text:
        quality = "magic"
    elif game.NORMAL_LABEL in quality_text:
        quality = "normal"
    else:
        quality = "unknown"
    secondary_affixes = list(raw.secondary_affixes)
    legendary_power = raw.legendary_power
    socket_effects = list(raw.socket_effects)
    if legendary_power and game.GEM_LEVEL_MARKER in legendary_power.splitlines()[0]:
        for line in reversed(legendary_power.splitlines()):
            if line not in socket_effects:
                socket_effects.insert(0, line)
        legendary_power = None
    if raw.slot in game.JEWELRY_SLOTS and any(
        game.GEM_LEVEL_MARKER in effect for effect in socket_effects
    ):
        socket_effects = [
            effect for effect in socket_effects if effect != game.EMPTY_SOCKET
        ]
    if legendary_power and "\n" not in legendary_power and (
        legendary_power.startswith("+")
        or legendary_power.startswith(game.CRITICAL_DAMAGE_PREFIX)
    ):
        if legendary_power not in socket_effects:
            socket_effects.append(legendary_power)
        legendary_power = None
    if not is_set:
        if legendary_power == set_name or (
            legendary_power
            and "\n" in legendary_power
            and all(
                line.strip().startswith("+") for line in legendary_power.splitlines()
            )
        ):
            legendary_power = None
        set_name = None
        if legendary_power is None:
            # ponytail: recover this follower effect; extend only for observed misreads.
            radiating = [
                affix for affix in secondary_affixes if game.EMANATES_MARKER in affix
            ]
            if len(radiating) == 1:
                legendary_power = radiating[0]
        if legendary_power in secondary_affixes:
            secondary_affixes.remove(legendary_power)
    return Item(
        role=role,
        owner=owner,
        name=name,
        kind="gem" if is_gem else "equipment",
        quality=quality,
        is_set=is_set,
        slot=None if is_gem else raw.slot,
        armor=(
            None if is_gem or is_weapon or raw.slot in game.JEWELRY_SLOTS else raw.armor
        ),
        dps=raw.dps if is_weapon and raw.dps != 0 else None,
        primary_affixes=raw.primary_affixes,
        secondary_affixes=secondary_affixes,
        socket_effects=socket_effects,
        set_name=set_name,
        legendary_power=None if is_set else legendary_power,
    )


def _ask(
    image: Path,
    *,
    prompt: str,
    schema: dict,
    num_predict: int,
    opener,
    model: str,
    keep_alive: int | str,
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": num_predict},
        "format": schema,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image.read_bytes()).decode("ascii")],
            }
        ],
    }
    request = Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=120) as response:
        return json.loads(response.read())["message"]["content"]


def unload(*, opener=urlopen, model: str = MODEL) -> None:
    request = Request(
        OLLAMA_GENERATE_URL,
        data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=30) as response:
        response.read()


def classify_view(image: Path, *, model: str = MODEL) -> ViewContext:
    with TemporaryDirectory() as root:
        header = Path(root) / "headers.png"
        with Image.open(image) as source:
            if source.size != REFERENCE_SIZE:
                return ViewContext()
            # ponytail: calibrated red window frames; extend for other resolutions/themes.
            titles = {}
            for label, box, frame in zip(ViewTitles.model_fields, HEADER_BANDS, HEADER_FRAMES):
                pixels = list(source.convert("RGB").crop(frame).get_flattened_data())
                present = sum(
                    r > FRAME_RED_MINIMUM
                    and r > FRAME_RED_DOMINANCE * g
                    and r > FRAME_RED_DOMINANCE * b
                    for r, g, b in pixels
                ) / len(pixels) >= FRAME_RED_FRACTION
                titles[label] = ""
                if present:
                    source.crop(box).save(header)
                    titles[label] = ViewTitle.model_validate_json(_ask(
                        header, prompt=VIEW_PROMPT, schema=ViewTitle.model_json_schema(),
                        num_predict=64, opener=urlopen, model=model, keep_alive="5m")).title
        return ViewTitles(**titles).context()


def analyze(
    image: Path,
    *,
    model: str = MODEL,
    screen_type: Literal[
        "player_stats", "materials", "follower_stats", "follower_item"
    ] | None = None,
    unload_model: bool = True,
) -> Recognition:
    if screen_type is not None:
        with Image.open(image) as source:
            if source.size != REFERENCE_SIZE:
                raise ValueError(f"guided screen types require a {REFERENCE_SIZE[0]}x{REFERENCE_SIZE[1]} screenshot")
    try:
        context = classify_view(image, model=model) if screen_type is None else ViewContext()
        if screen_type is None and context.screen_type != "items_or_other":
            screen_type = context.screen_type
        if screen_type is None:
            layout = locate_with_fallback(image, model=model, keep_alive="5m")
        else:
            kind, owner, role, box = GUIDED_PANELS[screen_type]
            layout = Layout(
                screen_type=screen_type,
                panels=[
                    Panel(
                        kind=kind,
                        role=role,
                        owner=owner,
                        title=screen_type,
                        box_1000=box,
                    )
                ],
                confidence=1.0,
            )
        items = []
        stats = {}
        follower_stats = {}
        materials = {}
        with TemporaryDirectory() as root:
            for index, panel in enumerate(layout.panels):
                if panel.kind == "unknown":
                    continue
                crop = crop_panel(
                    image,
                    panel.box_1000,
                    Path(root) / f"panel-{index}.png",
                )
                if panel.kind == "item":
                    item = extract_item(
                            crop,
                            role=panel.role,
                            owner="follower" if context.follower_open else panel.owner,
                            model=model,
                            keep_alive="5m",
                        )
                    if panel.role == "candidate" and context.inventory_open:
                        # ponytail: observed stash-left/inventory-right tooltip geometry.
                        location = "stash" if context.stash_open and sum(panel.box_1000[::2]) < BOX_SCALE else "inventory"
                        item.observed_location = location
                    items.append(item)
                elif panel.kind == "materials":
                    materials.update(
                        extract_materials(crop, model=model, keep_alive="5m")
                    )
                elif panel.kind in {"player_stats", "follower_stats"}:
                    owner = "follower" if panel.kind == "follower_stats" else "player"
                    target = follower_stats if owner == "follower" else stats
                    target.update(
                        extract_stats(crop, owner=owner, model=model, keep_alive="5m")
                    )
        return Recognition(
            screen_type=layout.screen_type,
            panels=layout.panels,
            items=items,
            stats=stats,
            follower_stats=follower_stats,
            materials=materials,
            confidence=layout.confidence,
            source_images=[image.name],
        )
    finally:
        if unload_model:
            unload(model=model)


def analyze_many(
    images: list[Path],
    *,
    screen_type: Literal["player_stats", "materials", "follower_stats"],
    model: str = MODEL,
) -> Recognition:
    if not images or screen_type not in {"player_stats", "materials", "follower_stats"}:
        raise ValueError("multiple images require a stats or materials screen type")
    try:
        result = analyze(
            images[0], model=model, screen_type=screen_type, unload_model=False
        )
        for image in images[1:]:
            next_result = analyze(
                image, model=model, screen_type=screen_type, unload_model=False
            )
            result.stats.update(next_result.stats)
            result.follower_stats.update(next_result.follower_stats)
            result.materials.update(next_result.materials)
            result.panels.extend(next_result.panels)
            result.source_images.extend(next_result.source_images)
            result.confidence = min(result.confidence, next_result.confidence)
        return result
    finally:
        unload(model=model)


def main() -> None:
    parser = argparse.ArgumentParser(prog="d3coach")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_command = commands.add_parser("analyze")
    analyze_command.add_argument(
        "images", nargs="+", type=Path, help="Diablo III screenshot(s)"
    )
    analyze_command.add_argument("--model", default=MODEL)
    analyze_command.add_argument("--screen-type", choices=GUIDED_PANELS)
    analyze_command.add_argument("--state", type=Path, default=Path("data/state.json"))
    analyze_command.add_argument("--location", choices=["unknown", "inventory", "stash"], default="unknown")
    analyze_command.add_argument("--item-id", help="Existing item identity for a single-item screenshot")
    watch_command = commands.add_parser("watch")
    watch_command.add_argument("--queue", type=Path, default=Path("screenshots"))
    watch_command.add_argument("--results", type=Path, default=Path("data/results"))
    watch_command.add_argument("--model", default=MODEL)
    watch_command.add_argument("--state", type=Path, default=Path("data/state.json"))
    state_command = commands.add_parser("state", help="Inspect/update the local character state")
    state_command.add_argument("--file", type=Path, default=Path("data/state.json"))
    actions = state_command.add_subparsers(dest="state_action", required=True)
    actions.add_parser("show")
    configure = actions.add_parser("configure")
    configure.add_argument("--class", dest="character_class")
    configure.add_argument("--build")
    configure.add_argument("--season")
    configure.add_argument("--goal", action="append", help="Ordered goals; replaces the goal list")
    progress = actions.add_parser("progress")
    progress.add_argument("area", choices=["season_journey", "altar"])
    progress.add_argument("key")
    progress.add_argument("status", choices=["unknown", "todo", "done"])
    resource = actions.add_parser("resource")
    resource.add_argument("name", choices=["gold", "blood_shards"])
    resource.add_argument("amount", type=int)
    mark = actions.add_parser("item")
    mark.add_argument("id")
    mark.add_argument("--location", choices=["unknown", "inventory", "stash"])
    mark.add_argument("--souvenir", action="store_true")
    mark.add_argument("--cube-status", choices=["unknown", "needed", "not_needed"])
    equip = actions.add_parser("equip", help="Issue an equip recommendation and assume it is followed")
    equip.add_argument("id")
    equip.add_argument("--replace-id")
    ingest = actions.add_parser("ingest", help="Import saved recognition JSON without another GPU call")
    ingest.add_argument("result", type=Path)
    ingest.add_argument("--location", choices=["unknown", "inventory", "stash"], default="unknown")
    ingest.add_argument("--item-id")
    args = parser.parse_args()
    if args.command == "analyze":
        if len(args.images) == 1:
            result = analyze(
                args.images[0], model=args.model, screen_type=args.screen_type
            )
        else:
            if args.screen_type not in {"player_stats", "materials", "follower_stats"}:
                parser.error(
                    "multiple screenshots require --screen-type player_stats, "
                    "materials or follower_stats"
                )
            result = analyze_many(
                args.images, model=args.model, screen_type=args.screen_type
            )
        persist_recognition(args.state, result, location=args.location, item_id=args.item_id)
        print(result.model_dump_json(indent=2))
    elif args.command == "watch":
        watch(args.queue, args.results, model=args.model, state_path=args.state)
    else:
        with state_lock(args.file):
            state = load_state(args.file)
            if args.state_action == "configure":
                for field in ("character_class", "build", "season"):
                    if getattr(args, field) is not None:
                        setattr(state, field, getattr(args, field))
                if args.goal is not None:
                    state.goals = args.goal
            elif args.state_action == "progress":
                getattr(state, args.area)[args.key] = args.status
            elif args.state_action == "resource":
                if args.amount < 0:
                    parser.error("resource amount cannot be negative")
                state.resources[args.name] = args.amount
            elif args.state_action == "item":
                stored = state.items[args.id]
                if args.location:
                    stored.location = args.location
                if args.souvenir:
                    stored.souvenir = True
                if args.cube_status:
                    stored.cube_status = args.cube_status
            elif args.state_action == "equip":
                advice = recommend_equip(state, args.id, replace_id=args.replace_id)
                save_state(args.file, state)
                print(advice.model_dump_json(indent=2))
                return
            elif args.state_action == "ingest":
                update_state(state, Recognition.model_validate_json(args.result.read_text(encoding="utf-8")),
                             location=args.location, item_id=args.item_id)
            if args.state_action != "show":
                save_state(args.file, state)
            print(state.model_dump_json(indent=2))


def crop_panel(
    source: Path,
    box_1000: list[int],
    target: Path,
    *,
    padding: int = CROP_PADDING,
) -> Path:
    with Image.open(source) as image:
        width, height = image.size
        left = max(0, box_1000[0] - padding) * width // BOX_SCALE
        top = max(0, box_1000[1] - padding) * height // BOX_SCALE
        right = min(BOX_SCALE, box_1000[2] + padding) * width // BOX_SCALE
        bottom = min(BOX_SCALE, box_1000[3] + padding) * height // BOX_SCALE
        target.parent.mkdir(parents=True, exist_ok=True)
        image.crop((left, top, right, bottom)).save(target)
    return target


def claim_next(incoming: Path, processing: Path) -> Path | None:
    incoming.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    return images[0].replace(processing / images[0].name) if images else None


def process_next(queue: Path, results: Path, *, model: str = MODEL,
                 state_path: Path | None = None) -> bool:
    processing = queue / "processing"
    done = queue / "done"
    failed = queue / "failed"
    review = queue / "review"
    incoming = queue / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    pending = sorted(path for path in processing.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
    images = pending or sorted(path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        return False
    try:
        before = images[0].stat()
        sleep(0.5)
        after = images[0].stat()
    except FileNotFoundError:
        return False
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return False
    try:
        claimed = pending[0] if pending else images[0].replace(processing / images[0].name)
    except (PermissionError, FileNotFoundError):
        return False
    if claimed is None:
        return False
    done.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    try:
        recognition = analyze(claimed, model=model)
        results.mkdir(parents=True, exist_ok=True)
        target = results / f"{claimed.stem}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(recognition.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        if not (recognition.items or recognition.stats or recognition.follower_stats or recognition.materials):
            review.mkdir(parents=True, exist_ok=True)
            claimed.replace(review / claimed.name)
            print(f"REVIEW {claimed.name}: no usable data; result {target}", flush=True)
            return True
        persist_recognition(state_path or results.parent / "state.json", recognition)
        claimed.replace(done / claimed.name)
        print(f"OK {claimed.name}: {recognition.screen_type}, {len(recognition.items)} items; state updated", flush=True)
    except Exception as error:
        claimed.replace(failed / claimed.name)
        (failed / f"{claimed.stem}.error.txt").write_text(str(error), encoding="utf-8")
        print(f"ERROR {claimed.name}: {error}", flush=True)
    return True


def watch(
    queue: Path,
    results: Path,
    *,
    model: str = MODEL,
    poll_seconds: float = 1,
    state_path: Path | None = None,
) -> None:
    print(f"Input: {(queue / 'incoming').resolve()}\nState: {(state_path or results.parent / 'state.json').resolve()}\nSerial processing; Ctrl+C stops the watcher.", flush=True)
    with state_lock(queue / "watcher", timeout=0):
        while True:
            if not process_next(queue, results, model=model, state_path=state_path):
                sleep(poll_seconds)


if __name__ == "__main__":
    main()
