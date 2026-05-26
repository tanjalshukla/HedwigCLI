from __future__ import annotations

from recipe_api.errors import AppError
from recipe_api.models import Recipe
from recipe_api.service import DEFAULT_SERVINGS, create_recipe, delete_recipe, get_recipe, list_recipes


def list_recipes_handler(query: dict) -> tuple[dict, int]:
    try:
        recipes = list_recipes()
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"recipes": [_recipe_to_dict(r) for r in recipes]}), 200


def get_recipe_handler(recipe_id: str) -> tuple[dict, int]:
    try:
        recipe = get_recipe(recipe_id)
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"recipe": _recipe_to_dict(recipe)}), 200


def create_recipe_handler(payload: dict) -> tuple[dict, int]:
    try:
        recipe = create_recipe(
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            ingredients=payload.get("ingredients", []),
            tags=payload.get("tags", []),
            author=payload.get("author", "anonymous"),
            servings=payload.get("servings", DEFAULT_SERVINGS),
            prep_time_minutes=payload.get("prep_time_minutes"),
            difficulty=payload.get("difficulty"),
        )
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"recipe": _recipe_to_dict(recipe)}), 201


def delete_recipe_handler(recipe_id: str) -> tuple[dict, int]:
    try:
        delete_recipe(recipe_id)
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"deleted": True}), 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def _recipe_to_dict(recipe: Recipe) -> dict:
    return {
        "id": recipe.id,
        "title": recipe.title,
        "description": recipe.description,
        "ingredients": [
            {"name": ing.name, "amount": ing.amount}
            for ing in recipe.ingredients
        ],
        "tags": recipe.tags,
        "author": recipe.author,
        "servings": recipe.servings,
        "prep_time_minutes": recipe.prep_time_minutes,
        "difficulty": recipe.difficulty,
    }
