"""
Recipe API development server.

Run with:
    python server.py

Then open http://localhost:5001 in a browser.
The page auto-refreshes every 3 seconds so changes made with Hedwig
appear immediately after the agent writes the file.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, request, jsonify, render_template_string
from recipe_api.service import (
    list_recipes,
    get_recipe,
    create_recipe,
    delete_recipe,
)
from recipe_api.errors import AppError

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Recipe title → local image filename. Lives in server.py rather than the
# Recipe dataclass so booth visitors editing models.py / store.py don't
# disturb it. Fallback: cards without a match render with no image.
_RECIPE_IMAGES: dict[str, str] = {
    "Pasta Carbonara": "pasta-carbonara.jpg",
    "Roasted Chicken with Herbs": "roasted-chicken.jpg",
    "Chocolate Chip Cookies": "chocolate-cookies.jpg",
    "Avocado Toast": "avocado-toast.jpg",
}


# ── HTML frontend ──────────────────────────────────────────────────────────────

_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Recipe API · Hedwig Demo</title>
<meta http-equiv="refresh" content="3">
<style>
  /* Booth-tuned palette: shares the CLI's cyan/dark visual language so
     the web surface and terminal feel like one product. */
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0e27;
    color: #e6f1ff;
    padding: 32px 24px;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 28px;
    border-bottom: 2px solid #00d9ff;
    padding-bottom: 14px;
  }
  header h1 {
    font-size: 24px;
    font-weight: 700;
    color: #00d9ff;
  }
  header .sub {
    font-size: 13px;
    color: #8a9bb8;
  }
  .refresh-note {
    font-size: 11px;
    color: #6a7a96;
    margin-left: auto;
    font-style: italic;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 18px;
  }
  .card {
    background: #141a36;
    border-radius: 10px;
    padding: 18px;
    box-shadow: 0 2px 12px rgba(0, 217, 255, 0.08);
  }
  .card img.recipe-photo {
    width: calc(100% + 36px);
    margin: -18px -18px 14px -18px;
    height: 160px;
    object-fit: cover;
    display: block;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
  }
  .card h2 {
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 6px;
    color: #ffffff;
  }
  .card .description {
    font-size: 13px;
    color: #c0cce0;
    margin-bottom: 10px;
    line-height: 1.45;
  }
  .card .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-bottom: 10px;
  }
  .tag {
    background: rgba(0, 217, 255, 0.08);
    color: #00d9ff;
    border: 1px solid rgba(0, 217, 255, 0.3);
    border-radius: 12px;
    font-size: 11px;
    padding: 2px 8px;
    font-weight: 600;
  }
  .ingredients {
    font-size: 12px;
    color: #c0cce0;
    margin-bottom: 10px;
  }
  .ingredients li {
    padding: 2px 0;
    padding-left: 12px;
    position: relative;
  }
  .ingredients li::before {
    content: "·";
    position: absolute;
    left: 0;
    color: #00d9ff;
  }
  .meta {
    font-size: 11px;
    color: #8a9bb8;
    border-top: 1px solid rgba(0, 217, 255, 0.15);
    padding-top: 8px;
    margin-top: 8px;
  }
  .meta .field {
    display: flex;
    justify-content: space-between;
    padding: 1px 0;
  }
  .meta .field .key { color: #6a7a96; }
  .meta .field .value { color: #c0cce0; font-weight: 600; }
  .empty {
    text-align: center;
    padding: 60px 20px;
    color: #6a7a96;
    font-size: 15px;
  }
  .count {
    font-size: 13px;
    color: #8a9bb8;
    margin-bottom: 18px;
  }
  .error-banner {
    background: rgba(255, 80, 80, 0.1);
    border: 1px solid rgba(255, 80, 80, 0.4);
    border-radius: 6px;
    padding: 12px 16px;
    margin-bottom: 18px;
    font-size: 13px;
    color: #ff8080;
  }
</style>
</head>
<body>
<header>
  <h1>🦔 Recipe API</h1>
  <span class="sub">Hedwig demo · build features live</span>
  <span class="refresh-note">auto-refreshes every 3s</span>
</header>

{% if error %}
<div class="error-banner">{{ error }}</div>
{% endif %}

<div class="count">{{ recipes|length }} recipe{{ 's' if recipes|length != 1 else '' }}</div>

{% if recipes %}
<div class="grid">
{% for r in recipes %}
<div class="card">
  {% if r.image_url %}
  <img class="recipe-photo" src="{{ r.image_url }}" alt="{{ r.title }}">
  {% endif %}
  <h2>{{ r.title }}</h2>
  {% if r.description %}
  <p class="description">{{ r.description }}</p>
  {% endif %}
  {% if r.tags %}
  <div class="tags">
    {% for tag in r.tags %}<span class="tag">{{ tag }}</span>{% endfor %}
  </div>
  {% endif %}
  {% if r.ingredients %}
  <ul class="ingredients">
    {% for ing in r.ingredients %}
    <li>{{ ing.amount }} {{ ing.name }}</li>
    {% endfor %}
  </ul>
  {% endif %}
  <div class="meta">
    <div class="field"><span class="key">id</span><span class="value">{{ r.id }}</span></div>
    <div class="field"><span class="key">author</span><span class="value">{{ r.author }}</span></div>
    {% for key, value in r.items() %}
      {% if key not in ['id', 'title', 'description', 'ingredients', 'tags', 'author', 'image_url'] and value is not none and value != '' and value != [] %}
      <div class="field"><span class="key">{{ key }}</span><span class="value">{{ value }}</span></div>
      {% endif %}
    {% endfor %}
  </div>
</div>
{% endfor %}
</div>
{% else %}
<div class="empty">No recipes yet — add some with the API or ask Hedwig to seed data.</div>
{% endif %}

</body>
</html>
"""


@app.route("/")
def index():
    error = None
    recipes = []
    try:
        recipes = [_to_dict(r) for r in list_recipes()]
    except Exception as exc:
        error = str(exc)
    return render_template_string(_HTML, recipes=recipes, error=error)


# ── JSON API ───────────────────────────────────────────────────────────────────

@app.route("/recipes", methods=["GET"])
def api_list():
    try:
        recipes = list_recipes()
        return jsonify({"ok": True, "data": {"recipes": [_to_dict(r) for r in recipes]}})
    except AppError as e:
        return jsonify(e.to_response()), e.status_code


@app.route("/recipes/<recipe_id>", methods=["GET"])
def api_get(recipe_id):
    try:
        recipe = get_recipe(recipe_id)
        return jsonify({"ok": True, "data": {"recipe": _to_dict(recipe)}})
    except AppError as e:
        return jsonify(e.to_response()), e.status_code


@app.route("/recipes", methods=["POST"])
def api_create():
    payload = request.get_json(silent=True) or {}
    try:
        recipe = create_recipe(
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            ingredients=payload.get("ingredients", []),
            tags=payload.get("tags", []),
            author=payload.get("author", "anonymous"),
        )
        return jsonify({"ok": True, "data": {"recipe": _to_dict(recipe)}}), 201
    except AppError as e:
        return jsonify(e.to_response()), e.status_code
    except TypeError as e:
        return jsonify({"ok": False, "error": {"code": "bad_request", "message": str(e)}}), 400


@app.route("/recipes/<recipe_id>", methods=["DELETE"])
def api_delete(recipe_id):
    try:
        delete_recipe(recipe_id)
        return jsonify({"ok": True, "data": {"deleted": True}})
    except AppError as e:
        return jsonify(e.to_response()), e.status_code


def _to_dict(recipe) -> dict:
    """Serialize a Recipe (dataclass, possibly with slots) to a JSON-friendly dict."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(recipe):
        data = asdict(recipe)
    elif hasattr(recipe, "__dict__"):
        data = dict(recipe.__dict__)
    else:
        data = dict(recipe)
    image = _RECIPE_IMAGES.get(data.get("title", ""))
    if image:
        data["image_url"] = f"/static/recipes/{image}"
    return data


if __name__ == "__main__":
    print()
    print("  🦔 Recipe API server")
    print("  Open http://localhost:5001 in your browser")
    print("  Page auto-refreshes every 3s — changes appear live")
    print("  Press Ctrl+C to stop")
    print()
    app.run(port=5001, debug=True, use_reloader=True)
