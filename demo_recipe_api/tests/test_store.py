from __future__ import annotations

from recipe_api.store import search_by_tag


def test_search_by_tag_returns_matching_recipes() -> None:
    results = search_by_tag("italian")
    ids = [r.id for r in results]
    assert "recipe-1" in ids


def test_search_by_tag_excludes_non_matching_recipes() -> None:
    results = search_by_tag("italian")
    ids = [r.id for r in results]
    assert "recipe-3" not in ids
    assert "recipe-4" not in ids


def test_search_by_tag_returns_empty_for_unknown_tag() -> None:
    results = search_by_tag("nonexistent-tag-xyz")
    assert results == []


def test_search_by_tag_is_case_insensitive() -> None:
    lower = search_by_tag("italian")
    upper = search_by_tag("ITALIAN")
    mixed = search_by_tag("Italian")
    assert [r.id for r in lower] == [r.id for r in upper] == [r.id for r in mixed]


def test_search_by_tag_single_result() -> None:
    # "breakfast" appears only on recipe-4 in seed data
    results = search_by_tag("breakfast")
    assert len(results) == 1
    assert results[0].id == "recipe-4"
