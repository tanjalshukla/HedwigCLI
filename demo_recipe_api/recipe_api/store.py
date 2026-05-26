from __future__ import annotations

from recipe_api.models import Ingredient, Recipe

_RECIPES: dict[str, Recipe] = {
    "recipe-1": Recipe(
        id="recipe-1",
        title="Pasta Carbonara",
        description=(
            "A classic Roman pasta dish with a rich, creamy sauce made from eggs, "
            "Pecorino Romano, and guanciale. No cream needed — the emulsion does the work."
        ),
        ingredients=[
            Ingredient(name="spaghetti", amount="400g"),
            Ingredient(name="guanciale or pancetta", amount="200g"),
            Ingredient(name="large eggs", amount="4"),
            Ingredient(name="Pecorino Romano, finely grated", amount="100g"),
            Ingredient(name="freshly ground black pepper", amount="1 tsp"),
        ],
        tags=["italian", "pasta"],
        author="chef-marco",
        servings=4,
    ),
    "recipe-2": Recipe(
        id="recipe-2",
        title="Roasted Chicken with Herbs",
        description=(
            "A whole chicken roasted to golden perfection with a butter and herb rub. "
            "Simple enough for a weeknight, impressive enough for company."
        ),
        ingredients=[
            Ingredient(name="whole chicken", amount="1.5kg"),
            Ingredient(name="unsalted butter, softened", amount="4 tbsp"),
            Ingredient(name="fresh rosemary sprigs", amount="3"),
            Ingredient(name="garlic cloves, smashed", amount="4"),
            Ingredient(name="lemon, halved", amount="1"),
        ],
        tags=["chicken", "roasted"],
        author="chef-helen",
        servings=4,
    ),
    "recipe-3": Recipe(
        id="recipe-3",
        title="Chocolate Chip Cookies",
        description=(
            "Crispy edges, chewy centers, and plenty of chocolate chips. "
            "Brown butter adds a nutty depth that takes these over the top."
        ),
        ingredients=[
            Ingredient(name="all-purpose flour", amount="2¼ cups"),
            Ingredient(name="unsalted butter, browned and cooled", amount="1 cup"),
            Ingredient(name="brown sugar, packed", amount="¾ cup"),
            Ingredient(name="large eggs", amount="2"),
            Ingredient(name="semi-sweet chocolate chips", amount="2 cups"),
        ],
        tags=["baking", "dessert"],
        author="chef-priya",
        servings=24,
    ),
    "recipe-4": Recipe(
        id="recipe-4",
        title="Avocado Toast",
        description=(
            "Creamy smashed avocado on thick toasted sourdough, finished with flaky salt "
            "and chilli flakes. Ready in under 10 minutes."
        ),
        ingredients=[
            Ingredient(name="sourdough bread, sliced thick", amount="2 slices"),
            Ingredient(name="ripe avocados", amount="2"),
            Ingredient(name="lemon juice", amount="1 tbsp"),
            Ingredient(name="chilli flakes", amount="½ tsp"),
            Ingredient(name="flaky sea salt", amount="to taste"),
        ],
        tags=["breakfast", "vegetarian"],
        author="anonymous",
        servings=2,
    ),
}


def list_recipes() -> list[Recipe]:
    return list(_RECIPES.values())


def get_recipe(recipe_id: str) -> Recipe | None:
    return _RECIPES.get(recipe_id)


def save_recipe(recipe: Recipe) -> None:
    _RECIPES[recipe.id] = recipe


def remove_recipe(recipe_id: str) -> None:
    _RECIPES.pop(recipe_id, None)


def next_recipe_id() -> str:
    n = len(_RECIPES) + 1
    while f"recipe-{n}" in _RECIPES:
        n += 1
    return f"recipe-{n}"
