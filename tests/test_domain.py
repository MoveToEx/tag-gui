from __future__ import annotations

from pathlib import Path

import pytest

from tag_gui.domain import (
    ImageEntry,
    ReviewSession,
    TagOperation,
    TraversalSession,
    apply_tag_operation,
    filter_traversal_entries,
    normalize_tags,
    matching_tag_counts,
    parse_requested_tags,
    parse_tags,
    serialize_tags,
    tag_matches_pattern,
)


def test_parse_and_serialize_tags() -> None:
    assert parse_tags(" cat, dog, cat, , 猫 ") == ["cat", "dog", "猫"]
    assert serialize_tags(["dog", "Cat", "cat", "dog"]) == "Cat, cat, dog\n"
    assert serialize_tags([]) == "\n"


def test_tag_search_pattern_supports_star_only() -> None:
    assert tag_matches_pattern("cat", "cat")
    assert tag_matches_pattern("cathedral", "cat*")
    assert tag_matches_pattern("red_cat_large", "red*large")
    assert tag_matches_pattern("anything", "*")
    assert not tag_matches_pattern("Cat", "cat")
    assert not tag_matches_pattern("cat1", "cat?")


def test_matching_tag_counts_returns_distinct_tags_and_image_counts(
    tmp_path: Path,
) -> None:
    entries = [
        _entry(tmp_path, "one.jpg", ["cat", "cathedral", "dog"]),
        _entry(tmp_path, "two.jpg", ["cat", "category"]),
    ]

    assert matching_tag_counts(entries, "cat") == [("cat", 2)]
    assert matching_tag_counts(entries, "cat*") == [
        ("cat", 2),
        ("category", 1),
        ("cathedral", 1),
    ]
    assert matching_tag_counts(entries, "") == []


def test_requested_tags_must_not_be_empty() -> None:
    try:
        parse_requested_tags(" , ")
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:
        raise AssertionError("Expected empty input to be rejected")


def test_operations_are_case_sensitive_and_normalized() -> None:
    current = ["dog", "Cat"]
    assert apply_tag_operation(current, ["cat"], TagOperation.ADD) == [
        "Cat",
        "cat",
        "dog",
    ]
    assert apply_tag_operation(current, ["Cat"], TagOperation.DELETE) == ["dog"]
    assert apply_tag_operation(current, ["dog", "bird"], TagOperation.TOGGLE) == [
        "Cat",
        "bird",
    ]
    assert normalize_tags(["z", "a", "z"]) == ["a", "z"]


def test_add_and_delete_traversals_filter_requested_tags(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "complete.jpg", ["cat", "dog"]),
        _entry(tmp_path, "partial.jpg", ["cat"]),
        _entry(tmp_path, "none.jpg", []),
    ]

    add_entries = filter_traversal_entries(
        entries, TagOperation.ADD, ["cat", "dog"]
    )
    delete_entries = filter_traversal_entries(
        entries, TagOperation.DELETE, ["cat", "dog"]
    )

    assert [entry.image_path.name for entry in add_entries] == [
        "partial.jpg",
        "none.jpg",
    ]
    assert [entry.image_path.name for entry in delete_entries] == [
        "complete.jpg",
        "partial.jpg",
    ]


def test_delete_traversal_session_includes_partial_matches(tmp_path: Path) -> None:
    session = TraversalSession(
        [
            _entry(tmp_path, "complete.jpg", ["cat", "dog"]),
            _entry(tmp_path, "partial.jpg", ["cat"]),
            _entry(tmp_path, "none.jpg", ["bird"]),
        ],
        TagOperation.DELETE,
        ["cat", "dog"],
    )

    assert [item.image_path.name for item in session.items] == [
        "complete.jpg",
        "partial.jpg",
    ]
    assert session.eligible_for(0) == ["cat", "dog"]
    assert session.eligible_for(1) == ["cat"]


def test_toggle_traversal_does_not_filter(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "one.jpg", []), _entry(tmp_path, "two.jpg", ["cat"])]

    assert filter_traversal_entries(entries, TagOperation.TOGGLE, ["cat"])


def test_normalize_is_not_a_traversal_operation(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "normalize.jpg", ["z", "a"])]

    with pytest.raises(ValueError, match="not a traversal operation"):
        filter_traversal_entries(entries, TagOperation.NORMALIZE)
    with pytest.raises(ValueError, match="not a traversal operation"):
        TraversalSession(entries, TagOperation.NORMALIZE)


def test_traversal_session_uses_filtered_candidates(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "complete.jpg", ["cat", "dog"]),
        _entry(tmp_path, "partial.jpg", ["cat"]),
    ]

    session = TraversalSession(entries, TagOperation.ADD, ["cat", "dog"])

    assert [item.image_path.name for item in session.items] == ["partial.jpg"]


def test_add_and_delete_start_with_no_selected_tags(tmp_path: Path) -> None:
    add_session = TraversalSession(
        [_entry(tmp_path, "add.jpg", ["cat"])],
        TagOperation.ADD,
        ["cat", "dog"],
    )
    delete_session = TraversalSession(
        [_entry(tmp_path, "delete.jpg", ["cat", "dog"])],
        TagOperation.DELETE,
        ["cat", "dog"],
    )
    toggle_session = TraversalSession(
        [_entry(tmp_path, "toggle.jpg", ["cat"])],
        TagOperation.TOGGLE,
        ["cat", "dog"],
    )

    assert add_session.selected_for() == []
    assert delete_session.selected_for() == []
    assert toggle_session.selected_for() == ["cat"]


def test_toggle_traversal_defaults_to_existing_requested_tags(tmp_path: Path) -> None:
    session = TraversalSession(
        [_entry(tmp_path, "toggle.jpg", ["cat", "bird"])],
        TagOperation.TOGGLE,
        ["cat", "dog"],
    )

    assert session.selected_for() == ["cat"]
    assert session.eligible_for() == ["cat", "dog"]


def test_temporary_extra_tags_use_the_session_operation(tmp_path: Path) -> None:
    add_session = TraversalSession(
        [_entry(tmp_path, "add-extra.jpg", ["cat"])],
        TagOperation.ADD,
        ["base"],
    )
    delete_session = TraversalSession(
        [_entry(tmp_path, "delete-extra.jpg", ["cat", "dog"])],
        TagOperation.DELETE,
        ["cat"],
    )

    assert add_session.apply_current([], ["temporary"]) == ["cat", "temporary"]
    assert delete_session.apply_current([], ["dog"]) == ["cat"]


def _entry(tmp_path: Path, name: str, tags: list[str]) -> ImageEntry:
    image_path = tmp_path / name
    tag_path = tmp_path / f"{Path(name).stem}.txt"
    source = (", ".join(tags) + "\n").encode()
    return ImageEntry(image_path, tag_path, tags, source)


def test_traversal_revisiting_replaces_staged_toggle(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "a.jpg", ["cat"]), _entry(tmp_path, "b.jpg", [])]
    session = TraversalSession(entries, TagOperation.TOGGLE, ["cat", "dog"])

    assert session.apply_current(["cat", "dog"]) == ["dog"]
    assert session.move_next()
    session.skip_current()
    assert session.move_back()
    assert session.apply_current(["dog"]) == ["cat", "dog"]

    assert session.staged_changes()[0][1] == ["cat", "dog"]


def test_traversal_all_available_changes_uses_every_option(tmp_path: Path) -> None:
    add_session = TraversalSession(
        [_entry(tmp_path, "add.jpg", ["cat"])],
        TagOperation.ADD,
        ["cat", "dog", "bird"],
    )
    delete_session = TraversalSession(
        [_entry(tmp_path, "delete.jpg", ["cat", "dog", "bird"])],
        TagOperation.DELETE,
        ["cat", "dog"],
    )
    toggle_session = TraversalSession(
        [_entry(tmp_path, "toggle.jpg", ["cat"])],
        TagOperation.TOGGLE,
        ["cat", "dog"],
    )
    assert add_session.all_available_changes()[0][1] == ["bird", "cat", "dog"]
    assert delete_session.all_available_changes()[0][1] == ["bird"]
    assert toggle_session.all_available_changes()[0][1] == ["dog"]


def test_review_session_stages_deletions_and_advances_by_tag(tmp_path: Path) -> None:
    session = ReviewSession(
        [
            _entry(tmp_path, "first.jpg", ["cat", "dog"]),
            _entry(tmp_path, "second.jpg", ["bird"]),
        ]
    )

    assert session.current_tag == "cat"
    assert session.reviewed_tag_count == 0
    assert session.total_tag_count == 3
    session.keep_current()
    assert session.current_tag == "dog"
    assert session.reviewed_tag_count == 1
    session.delete_current()
    assert session.current_index == 1
    assert session.current_tag == "bird"
    assert session.staged_changes()[0][1] == ["cat"]

    middle = ReviewSession([_entry(tmp_path, "middle.jpg", ["a", "b", "c"])])
    middle.keep_current()
    middle.delete_current()
    assert middle.current_tag == "c"
    assert middle.move_back()
    assert middle.current_tag == "b"
    assert middle.current_tags == ["a", "c"]
    middle.keep_current()
    assert middle.current_tags == ["a", "b", "c"]
    session.keep_current()
    assert session.finished
    assert session.staged_changes()[0][1] == ["cat"]


def test_review_navigation_only_shifts_position(tmp_path: Path) -> None:
    session = ReviewSession(
        [
            _entry(tmp_path, "first.jpg", ["cat", "dog"]),
            _entry(tmp_path, "second.jpg", ["bird"]),
        ]
    )

    assert session.move_forward()
    assert session.current_tag == "dog"
    assert session.reviewed_tag_count == 0
    session.delete_current()
    assert session.current_tag == "bird"
    assert session.move_back()
    assert session.current_tag == "dog"
    assert session.current_tags == ["cat"]


def test_review_session_adds_extra_tags_as_kept(tmp_path: Path) -> None:
    session = ReviewSession([_entry(tmp_path, "extra.jpg", ["cat"])])

    assert session.add_kept_tags(["new", "cat"]) == ["new"]
    assert session.current_tags == ["cat", "new"]
    assert "new" in session.reviewed_tags[0]
    assert session.reviewed_tag_count == 0
    assert session.staged_changes()[0][1] == ["cat", "new"]
