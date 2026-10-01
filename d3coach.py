import argparse
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.request import Request, urlopen

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3-vl:8b-instruct"
LAYOUT_PROMPT = """Analysiere das Layout dieses deutschen Diablo-III-Screenshots.
Lokalisiere ausschließlich relevante Item-, Werte- und Material-Panels; transkribiere deren Inhalt noch nicht.
Ein Item-Panel mit der sichtbaren Überschrift AUSGERÜSTET hat role=equipped.
Ein daneben angezeigtes Vergleichsitem hat role=candidate. owner ist player oder follower.
Koordinaten in box_1000 sind ganze Zahlen von 0 bis 1000 relativ zum Vollbild.
Nutze unknown, wenn etwas nicht sicher sichtbar ist. Erfinde nichts."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Panel(StrictModel):
    kind: Literal["item", "player_stats", "materials", "follower_stats", "unknown"]
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    title: str
    box_1000: list[int] = Field(min_length=4, max_length=4)

    @field_validator("box_1000")
    @classmethod
    def validate_box(cls, box: list[int]) -> list[int]:
        if any(coordinate < 0 or coordinate > 1000 for coordinate in box):
            raise ValueError("box coordinates must be between 0 and 1000")
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("box must have positive width and height")
        return box


class Item(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    name: str
    quality: Literal[
        "normal",
        "magic",
        "rare",
        "legendary",
        "set",
        "ancient_legendary",
        "ancient_set",
        "primal_ancient",
        "unknown",
    ]
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    set_name: str | None
    legendary_power: str | None


class RawItem(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    name: str
    quality_text: str = Field(
        description="Exact colored line below the item name, e.g. 'Uraltes Set: Stiefel'"
    )
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    set_name: str | None = Field(
        description="Green set heading above the set-piece list; never the item name"
    )
    legendary_power: str | None = Field(
        description="Orange item-specific power only; never a green set-piece list or set bonus"
    )


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
    materials: dict[str, int]
    confidence: float = Field(ge=0, le=1)


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


def extract_item(
    image: Path,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> Item:
    prompt = f"""Extrahiere exakt das einzelne sichtbare deutsche Diablo-III-Item.
role={role} und owner={owner} sind vorgegeben.
Übernimm quality_text als exakte farbige Zeile unter dem Itemnamen.
Übernimm sichtbare Zahlen, Wertebereiche und Affix-Texte exakt.
set_name ist nur die grüne Set-Überschrift über der Set-Teileliste.
legendary_power ist nur ein orangefarbener itemspezifischer Effekt; grüne Setlisten und Set-Boni gehören nicht hinein.
Ignoriere Werteveränderungen beim Anlegen, Set-Bonus-Texte, Flavor-Text, Verkaufswert und Haltbarkeit. Erfinde nichts."""
    content = _ask(
        image,
        prompt=prompt,
        schema=RawItem.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return normalize_item(
        RawItem.model_validate_json(content), role=role, owner=owner
    )


def normalize_item(
    raw: RawItem,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
) -> Item:
    quality_text = raw.quality_text.casefold()
    set_name = raw.set_name
    known_quality = ("archaisch", "set", "legendär", "selten", "magisch", "normal")
    if not any(label in quality_text for label in known_quality) and set_name:
        if "set" in set_name.casefold():
            quality_text = set_name.casefold()
            set_name = None
        else:
            quality_text = "set"
    if "archaisch" in quality_text:
        quality = "primal_ancient"
    elif "set" in quality_text:
        quality = "ancient_set" if "uralt" in quality_text else "set"
    elif "legendär" in quality_text:
        quality = "ancient_legendary" if "uralt" in quality_text else "legendary"
    elif "selten" in quality_text:
        quality = "rare"
    elif "magisch" in quality_text:
        quality = "magic"
    elif "normal" in quality_text:
        quality = "normal"
    else:
        quality = "unknown"
    return Item(
        role=role,
        owner=owner,
        name=raw.name,
        quality=quality,
        slot=raw.slot,
        armor=raw.armor,
        dps=None if raw.armor is not None and raw.dps == 0 else raw.dps,
        primary_affixes=raw.primary_affixes,
        secondary_affixes=raw.secondary_affixes,
        set_name=set_name,
        legendary_power=(
            None if quality in {"set", "ancient_set"} else raw.legendary_power
        ),
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


def analyze(image: Path, *, model: str = MODEL) -> Recognition:
    try:
        layout = locate(image, model=model, keep_alive="5m")
        items = []
        with TemporaryDirectory() as root:
            for index, panel in enumerate(layout.panels):
                if panel.kind != "item":
                    continue
                crop = crop_panel(
                    image,
                    panel.box_1000,
                    Path(root) / f"panel-{index}.png",
                )
                items.append(
                    extract_item(
                        crop,
                        role=panel.role,
                        owner=panel.owner,
                        model=model,
                        keep_alive="5m",
                    )
                )
        return Recognition(
            screen_type=layout.screen_type,
            panels=layout.panels,
            items=items,
            stats={},
            materials={},
            confidence=layout.confidence,
        )
    finally:
        unload(model=model)


def main() -> None:
    parser = argparse.ArgumentParser(prog="d3coach")
    parser.add_argument("image", type=Path, help="Diablo III screenshot")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    print(analyze(args.image, model=args.model).model_dump_json(indent=2))


def crop_panel(
    source: Path,
    box_1000: list[int],
    target: Path,
    *,
    padding: int = 10,
) -> Path:
    with Image.open(source) as image:
        width, height = image.size
        left = max(0, box_1000[0] - padding) * width // 1000
        top = max(0, box_1000[1] - padding) * height // 1000
        right = min(1000, box_1000[2] + padding) * width // 1000
        bottom = min(1000, box_1000[3] + padding) * height // 1000
        target.parent.mkdir(parents=True, exist_ok=True)
        image.crop((left, top, right, bottom)).save(target)
    return target


def claim_next(incoming: Path, processing: Path) -> Path | None:
    processing.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    return images[0].replace(processing / images[0].name) if images else None


if __name__ == "__main__":
    main()
