import base64
import json
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
OLLAMA_URL = "http://localhost:11434/api/chat"
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
) -> Recognition:
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": 0,
        "options": {"temperature": 0, "num_predict": 512},
        "format": Layout.model_json_schema(),
        "messages": [
            {
                "role": "user",
                "content": LAYOUT_PROMPT,
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
        content = json.loads(response.read())["message"]["content"]
    return Layout.model_validate_json(content)


def claim_next(incoming: Path, processing: Path) -> Path | None:
    processing.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    return images[0].replace(processing / images[0].name) if images else None
