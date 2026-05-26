from __future__ import annotations

from recipe_api.errors import AppError
from recipe_api.models import Ingredient, Recipe
from recipe_api.store import get_recipe as load_recipe, list_recipes as load_recipes, next_recipe_id, remove_recipe, save_recipe

TITLE_MAX_LENGTH = 150
DEFAULT_SERVINGS = 4
VALID_DIFFICULTIES = {"easy", "medium", "hard"}


def list_recipes() -> list[Recipe]:
    """Return all recipes. No filtering — that is a visitor feature."""
    return load_recipes()


def get_recipe(recipe_id: str) -> Recipe:
    return _require_recipe(recipe_id)


def create_recipe(
    title: str,
    description: str,
    ingredients: list[dict],
    tags: list[str],
    author: str = "anonymous",
    servings: int = DEFAULT_SERVINGS,
    prep_time_minutes: int | None = None,
    difficulty: str | None = None,
) -> Recipe:
    _validate_title(title)
    parsed_ingredients = _validate_ingredients(ingredients)
    _validate_servings(servings)
    _validate_prep_time(prep_time_minutes)
    _validate_difficulty(difficulty)
    recipe = Recipe(
        id=next_recipe_id(),
        title=title.strip(),
        description=description.strip(),
        ingredients=parsed_ingredients,
        tags=[t.strip() for t in tags],
        author=author.strip() or "anonymous",
        servings=servings,
        prep_time_minutes=prep_time_minutes,
        difficulty=difficulty,
    )
    save_recipe(recipe)
    return recipe


def delete_recipe(recipe_id: str) -> None:
    _require_recipe(recipe_id)
    remove_recipe(recipe_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_recipe(recipe_id: str) -> Recipe:
    recipe = load_recipe(recipe_id)
    if recipe is None:
        raise AppError(
            code="recipe_not_found",
            message=f"Recipe '{recipe_id}' not found.",
            status_code=404,
        )
    return recipe


def _validate_title(title: str) -> None:
    if not isinstance(title, str):
        raise AppError(code="invalid_title_type", message="title must be a string")
    if not title.strip():
        raise AppError(code="empty_title", message="title cannot be empty")
    if len(title) > TITLE_MAX_LENGTH:
        raise AppError(
            code="title_too_long",
            message=f"title must be {TITLE_MAX_LENGTH} characters or fewer",
        )


def _validate_servings(servings: int) -> None:
    if not isinstance(servings, int) or isinstance(servings, bool):
        raise AppError(code="invalid_servings_type", message="servings must be an integer")
    if servings < 1:
        raise AppError(code="invalid_servings", message="servings must be at least 1")


def _validate_prep_time(prep_time_minutes: int | None) -> None:
    if prep_time_minutes is None:
        return
    if not isinstance(prep_time_minutes, int) or isinstance(prep_time_minutes, bool):
        raise AppError(
            code="invalid_prep_time_type",
            message="prep_time_minutes must be an integer",
        )
    if prep_time_minutes < 1:
        raise AppError(
            code="invalid_prep_time",
            message="prep_time_minutes must be at least 1",
        )


def _validate_difficulty(difficulty: str | None) -> None:
    if difficulty is None:
        return
    if not isinstance(difficulty, str):
        raise AppError(
            code="invalid_difficulty_type",
            message="difficulty must be a string",
        )
    if difficulty not in VALID_DIFFICULTIES:
        raise AppError(
            code="invalid_difficulty",
            message="difficulty must be one of: easy, medium, hard",
        )


def _validate_ingredients(raw: list[dict]) -> list[Ingredient]:
    if not raw:
        raise AppError(
            code="no_ingredients",
            message="at least one ingredient is required",
        )
    parsed: list[Ingredient] = []
    for i, item in enumerate(raw):
        name = item.get("name", "").strip() if isinstance(item.get("name"), str) else ""
        amount = item.get("amount", "").strip() if isinstance(item.get("amount"), str) else ""
        if not name:
            raise AppError(
                code="invalid_ingredient",
                message=f"ingredient at index {i} has an empty name",
            )
        if not amount:
            raise AppError(
                code="invalid_ingredient",
                message=f"ingredient at index {i} has an empty amount",
            )
        parsed.append(Ingredient(name=name, amount=amount))
    return parsed
