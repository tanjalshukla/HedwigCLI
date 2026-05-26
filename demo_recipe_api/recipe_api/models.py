from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Ingredient:
    name: str
    amount: str  # e.g. "2 cups", "1 tbsp", "3 large"


@dataclass(slots=True)
class Tag:
    name: str


@dataclass(slots=True)
class Recipe:
    id: str
    title: str
    description: str
    ingredients: list[Ingredient]
    tags: list[str]
    author: str = "anonymous"
    servings: int = 4
    prep_time_minutes: int | None = None  # how long the recipe takes to make
    difficulty: str | None = None  # easy, medium, or hard
