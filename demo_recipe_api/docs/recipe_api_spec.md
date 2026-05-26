# Recipe API Spec

This document describes the baseline API surface, data model, error format, and design conventions for the recipe fixture. Pass it as `--spec` context in Hedwig runs to give the agent accurate grounding.

---

## API surface — baseline endpoints

All handlers live in `recipe_api/api.py`. They take plain dicts and return `(dict, int)` — no web framework, no HTTP transport.

### list_recipes_handler

```
list_recipes_handler(query: dict) -> (dict, 200)
```

Parameters (all optional, passed via `query` dict):
- none at baseline — filtering, pagination, and search are intentionally absent

Response:
```json
{
  "ok": true,
  "data": {
    "recipes": [
      {
        "id": "rec-1",
        "title": "Pasta Carbonara",
        "description": "A classic Roman pasta dish.",
        "author": "alice",
        "ingredients": [
          {"name": "spaghetti", "amount": "200g"},
          {"name": "eggs", "amount": "3"}
        ],
        "tags": ["italian", "pasta"]
      }
    ]
  }
}
```

### get_recipe_handler

```
get_recipe_handler(recipe_id: str) -> (dict, 200) | (dict, 404)
```

Response on success: same recipe shape as above, wrapped in `{"ok": true, "data": {"recipe": {...}}}`.
Response on not-found: `AppError` with code `recipe_not_found`, status 404.

### create_recipe_handler

```
create_recipe_handler(payload: dict) -> (dict, 201) | (dict, 400)
```

Required payload fields: `title` (str), `author` (str)
Optional payload fields: `description` (str, default `""`), `ingredients` (list, default `[]`), `tags` (list, default `[]`)

Response on success: `{"ok": true, "data": {"recipe": {...}}}` with status 201.
Validation errors: `AppError` with appropriate code and status 400.

### delete_recipe_handler

```
delete_recipe_handler(recipe_id: str) -> (dict, 200) | (dict, 404)
```

Response on success: `{"ok": true, "data": {"deleted": true}}` with status 200.
Response on not-found: `AppError` with code `recipe_not_found`, status 404.

---

## Data model

### Recipe

```python
@dataclass(slots=True)
class Recipe:
    id: str
    title: str
    description: str = ""
    author: str = ""
    ingredients: list[Ingredient] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
```

Field constraints enforced at baseline:
- `title`: required, non-empty string, max 200 characters
- `author`: required, non-empty string
- `description`: optional, no length limit at baseline (length validation is a Tier 1 visitor feature)
- `ingredients`: list of `Ingredient`, may be empty
- `tags`: list of `Tag`, may be empty

### Ingredient

```python
@dataclass(slots=True)
class Ingredient:
    name: str    # e.g. "spaghetti"
    amount: str  # e.g. "200g" — free text, no parsing
```

### Tag

```python
@dataclass(slots=True)
class Tag:
    name: str    # e.g. "italian"
```

Tags are stored as simple strings wrapped in the Tag dataclass. Tag deduplication and normalization are not implemented at baseline (visitor features).

---

## Error format

All user-facing errors are `AppError` instances from `recipe_api/errors.py`:

```python
@dataclass(slots=True)
class AppError(Exception):
    code: str          # machine-readable error code, e.g. "recipe_not_found"
    message: str       # human-readable message
    status_code: int = 400
```

Error response shape (returned by `AppError.to_response()`):

```json
{
  "ok": false,
  "error": {
    "code": "recipe_not_found",
    "message": "Recipe 'rec-99' not found."
  }
}
```

Established error codes at baseline:
- `empty_title` — title is blank or whitespace-only
- `title_too_long` — title exceeds 200 characters
- `empty_author` — author is blank or whitespace-only
- `recipe_not_found` — no recipe with the given ID (status 404)
- `invalid_ingredient` — malformed ingredient in create payload

---

## Design constraints

**No web framework.** There is no Flask, FastAPI, Django, or any HTTP framework in this fixture. Handlers are plain Python functions. Do not introduce a web framework. If a visitor feature requires routing, implement a minimal dispatcher in `api.py` rather than pulling in a framework.

**In-memory store.** Recipes are stored in a module-level dict in `recipe_api/store.py` (or equivalent). There is no database. Do not introduce SQLAlchemy, sqlite3, or any persistence layer. All state is reset between test runs.

**Framework-free handlers.** Handler signatures are `handler(query: dict) -> (dict, int)` or `handler(resource_id: str, payload: dict) -> (dict, int)`. Do not change this signature pattern without an explicit check-in.

**AppError for all validation.** Raise `AppError` with an explicit `code` string for every user-facing failure. Do not raise bare `ValueError` or `TypeError` from service or handler functions.

---

## Code conventions

**`_validate_*` helpers in service.py.** Validation functions are named `_validate_<field>(value)` and raise `AppError` directly. They do not return the validated value (they either pass or raise). Pattern:

```python
def _validate_title(title: str) -> None:
    if not isinstance(title, str):
        raise AppError(code="invalid_title_type", message="title must be a string")
    if not title.strip():
        raise AppError(code="empty_title", message="title cannot be empty")
    if len(title) > 200:
        raise AppError(code="title_too_long", message="title must be 200 characters or fewer")
```

**`_require_*` for existence checks in service.py.** Functions that look up a resource and raise 404 on miss are named `_require_<resource>(id)` and return the resource:

```python
def _require_recipe(recipe_id: str) -> Recipe:
    recipe = get_recipe(recipe_id)
    if recipe is None:
        raise AppError(code="recipe_not_found", message=f"Recipe '{recipe_id}' not found.", status_code=404)
    return recipe
```

**`_ok()` wrapper in api.py.** All successful responses go through `_ok(data)`:

```python
def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}
```

Do not construct `{"ok": True, "data": ...}` inline — use `_ok()`.

