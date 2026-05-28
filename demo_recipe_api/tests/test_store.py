from __future__ import annotations

from recipe_api.store import get_recipe, list_recipes, next_recipe_id


def test_list_recipes_returns_seed_data() -> None:
    recipes = list_recipes()
    recipe_ids = {recipe.id for recipe in recipes}
    assert {"recipe-1", "recipe-2", "recipe-3", "recipe-4"}.issubset(recipe_ids)


def test_get_recipe_returns_seed_recipe() -> None:
    recipe = get_recipe("recipe-1")
    assert recipe is not None
    assert recipe.title == "Pasta Carbonara"


def test_get_recipe_returns_none_for_missing_recipe() -> None:
    assert get_recipe("recipe-9999") is None


def test_next_recipe_id_skips_existing_seed_ids() -> None:
    existing_ids = {recipe.id for recipe in list_recipes()}
    next_id = next_recipe_id()
    assert next_id.startswith("recipe-")
    assert next_id not in existing_ids
