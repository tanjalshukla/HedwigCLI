from __future__ import annotations

from recipe_api.api import (
    create_recipe_handler,
    delete_recipe_handler,
    get_recipe_handler,
    list_recipes_handler,
)

_VALID_PAYLOAD = {
    "title": "Simple Tomato Soup",
    "description": "A warming, velvety tomato soup from pantry staples.",
    "ingredients": [
        {"name": "canned whole tomatoes", "amount": "2 x 400g tins"},
        {"name": "yellow onion, diced", "amount": "1 large"},
        {"name": "vegetable stock", "amount": "500ml"},
    ],
    "tags": ["soup", "vegetarian"],
    "author": "test-user",
}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_returns_four_seed_recipes() -> None:
    body, status = list_recipes_handler({})
    assert status == 200
    assert body["ok"] is True
    assert len(body["data"]["recipes"]) == 4


def test_list_recipe_shape() -> None:
    body, _ = list_recipes_handler({})
    recipe = body["data"]["recipes"][0]
    assert "id" in recipe
    assert "title" in recipe
    assert "description" in recipe
    assert "ingredients" in recipe
    assert "tags" in recipe
    assert "author" in recipe
    assert "servings" in recipe


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_succeeds_with_valid_payload() -> None:
    body, status = create_recipe_handler(_VALID_PAYLOAD)
    assert status == 201
    assert body["ok"] is True
    assert body["data"]["recipe"]["title"] == "Simple Tomato Soup"
    assert body["data"]["recipe"]["author"] == "test-user"
    assert len(body["data"]["recipe"]["ingredients"]) == 3


def test_create_uses_default_servings_when_omitted() -> None:
    body, status = create_recipe_handler(_VALID_PAYLOAD)
    assert status == 201
    assert body["data"]["recipe"]["servings"] == 4


def test_create_accepts_explicit_servings() -> None:
    payload = {**_VALID_PAYLOAD, "servings": 6}
    body, status = create_recipe_handler(payload)
    assert status == 201
    assert body["data"]["recipe"]["servings"] == 6


def test_create_rejects_zero_servings() -> None:
    payload = {**_VALID_PAYLOAD, "servings": 0}
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "invalid_servings"


def test_create_rejects_negative_servings() -> None:
    payload = {**_VALID_PAYLOAD, "servings": -1}
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "invalid_servings"


def test_create_rejects_empty_title() -> None:
    payload = {**_VALID_PAYLOAD, "title": "   "}
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "empty_title"


def test_create_rejects_title_too_long() -> None:
    payload = {**_VALID_PAYLOAD, "title": "x" * 151}
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "title_too_long"


def test_create_rejects_zero_ingredients() -> None:
    payload = {**_VALID_PAYLOAD, "ingredients": []}
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "no_ingredients"


def test_create_rejects_ingredient_with_empty_name() -> None:
    payload = {
        **_VALID_PAYLOAD,
        "ingredients": [{"name": "", "amount": "1 cup"}],
    }
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "invalid_ingredient"


def test_create_rejects_ingredient_with_empty_amount() -> None:
    payload = {
        **_VALID_PAYLOAD,
        "ingredients": [{"name": "flour", "amount": ""}],
    }
    body, status = create_recipe_handler(payload)
    assert status == 400
    assert body["error"]["code"] == "invalid_ingredient"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_returns_specific_recipe() -> None:
    body, status = get_recipe_handler("recipe-1")
    assert status == 200
    assert body["ok"] is True
    assert body["data"]["recipe"]["id"] == "recipe-1"
    assert body["data"]["recipe"]["title"] == "Pasta Carbonara"


def test_get_recipe_includes_servings() -> None:
    body, status = get_recipe_handler("recipe-1")
    assert status == 200
    assert body["data"]["recipe"]["servings"] == 4


def test_get_returns_404_for_missing_recipe() -> None:
    body, status = get_recipe_handler("recipe-9999")
    assert status == 404
    assert body["ok"] is False
    assert body["error"]["code"] == "recipe_not_found"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_succeeds() -> None:
    # Create a fresh recipe so we don't interfere with seed data used by other tests
    create_body, _ = create_recipe_handler({
        "title": "Recipe to Delete",
        "description": "Temporary.",
        "ingredients": [{"name": "water", "amount": "1 cup"}],
        "tags": [],
    })
    recipe_id = create_body["data"]["recipe"]["id"]
    body, status = delete_recipe_handler(recipe_id)
    assert status == 200
    assert body["data"]["deleted"] is True


def test_delete_returns_404_for_missing_recipe() -> None:
    body, status = delete_recipe_handler("recipe-9999")
    assert status == 404
    assert body["error"]["code"] == "recipe_not_found"
