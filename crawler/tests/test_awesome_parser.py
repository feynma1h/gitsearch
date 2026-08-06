"""Tests for the awesome-list parser and the miner's aggregation policy.

The parser is pure text -> structures, so these tests are the place
where every format quirk found in real lists gets pinned down.
"""

from src.awesome_parser import MinedEntry, parse_awesome_readme
from src.mine_awesome import aggregate_entries, select_payload


# ---------------------------------------------------------------------------
# parse_awesome_readme
# ---------------------------------------------------------------------------


def test_basic_entry_with_dash_description():
    md = (
        "# Awesome Python\n"
        "## Machine Learning\n"
        "- [scikit-learn](https://github.com/scikit-learn/scikit-learn) - "
        "The most popular Python library for Machine Learning.\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.full_name == "scikit-learn/scikit-learn"
    assert entry.alias == "scikit-learn"
    assert entry.description == (
        "The most popular Python library for Machine Learning."
    )
    # h1 loses the word "awesome"; the rest of the trail survives.
    assert entry.categories == ("Python", "Machine Learning")


def test_heading_trail_pops_on_sibling_and_filters_plumbing():
    md = (
        "# Awesome Deep Learning\n"
        "## Contents\n"
        "- [pytorch](https://github.com/pytorch/pytorch)\n"
        "## Researchers\n"
        "### People\n"
        "## Frameworks\n"
        "- [pytorch](https://github.com/pytorch/pytorch) - Tensors.\n"
    )
    entries = parse_awesome_readme(md)
    # Under "Contents" the trail is just the de-awesomed h1.
    assert entries[0].categories == ("Deep Learning",)
    # "Researchers" and its h3 must be gone once the sibling h2 arrives.
    assert entries[1].categories == ("Deep Learning", "Frameworks")


def test_numbered_item_with_description_inside_anchor():
    md = (
        "### Frameworks\n"
        "45. [PyTorch - Tensors and Dynamic neural networks in Python with "
        "strong GPU acceleration](https://github.com/pytorch/pytorch)\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.alias == "PyTorch"
    assert entry.description.startswith("Tensors and Dynamic")
    assert entry.categories == ("Frameworks",)


def test_deep_links_and_reserved_owners_are_rejected():
    md = (
        "- [file](https://github.com/o/r/blob/main/README.md) - A file.\n"
        "- [dir](https://github.com/o/r/tree/main/docs) - A dir.\n"
        "- [topic](https://github.com/topics/python) - A topic page.\n"
        "- [ok](https://github.com/o/r) - The repo itself.\n"
    )
    entries = parse_awesome_readme(md)
    assert [e.full_name for e in entries] == ["o/r"]


def test_git_suffix_fragment_and_trailing_slash_normalise():
    md = (
        "- [a](https://github.com/o/r.git) - X.\n"
        "- [b](https://github.com/o/r/) - Y.\n"
        "- [c](https://github.com/o/r#readme) - Z.\n"
    )
    entries = parse_awesome_readme(md)
    assert {e.full_name for e in entries} == {"o/r"}
    assert len(entries) == 3


def test_self_reference_is_suppressed():
    md = "- [this list](https://github.com/me/awesome-me) - Badge link.\n"
    assert parse_awesome_readme(md, "me/awesome-me") == []


def test_fenced_code_blocks_are_skipped():
    md = (
        "```\n"
        "- [fake](https://github.com/not/real) - inside a fence\n"
        "```\n"
        "- [real](https://github.com/o/r) - outside.\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.full_name == "o/r"


def test_description_goes_to_first_link_only():
    md = (
        "- [main](https://github.com/o/main) - The tool "
        "(fork of [orig](https://github.com/o/orig)).\n"
    )
    entries = parse_awesome_readme(md)
    by_name = {e.full_name: e for e in entries}
    assert by_name["o/main"].description is not None
    assert by_name["o/orig"].description is None


def test_table_row_yields_entry():
    md = (
        "| Name | Notes |\n"
        "| [ripgrep](https://github.com/BurntSushi/ripgrep) | fast grep |\n"
    )
    entries = parse_awesome_readme(md)
    assert entries[0].full_name == "BurntSushi/ripgrep"
    assert entries[0].description == "fast grep"


def test_selfhosted_style_tags_and_link_parens_are_stripped():
    md = (
        "- [Tandoor](https://github.com/TandoorRecipes/recipes) - Recipe "
        "manager. ([Demo](https://x), [Source Code](https://y)) `MIT` `Python`\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.description == "Recipe manager."


def test_heading_badges_are_stripped():
    md = (
        "# Awesome ML [![Awesome](https://img)](https://github.com/s/a)\n"
        "- [x](https://github.com/o/r) - Y.\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.categories == ("ML",)


def test_prose_and_heading_links_do_not_yield_entries():
    md = (
        "See [pytorch](https://github.com/pytorch/pytorch) in prose.\n"
        "## About [react](https://github.com/facebook/react)\n"
    )
    assert parse_awesome_readme(md) == []


def test_leading_label_parens_and_trailing_bracket_tags_are_stripped():
    md = (
        "- [x](https://github.com/o/r) - _(label: good first issue)_ "
        "A modern ebook reader. [MIT] [website]\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.description == "A modern ebook reader."


def test_fragment_descriptions_are_dropped():
    md = "- [x](https://github.com/o/r) - list.\n"
    (entry,) = parse_awesome_readme(md)
    assert entry.description is None


def test_sentence_length_anchor_without_dash_becomes_description():
    md = "- [A curated collection of fine tuning helpers](https://github.com/o/r)\n"
    (entry,) = parse_awesome_readme(md)
    assert entry.alias is None
    assert entry.description == "A curated collection of fine tuning helpers"


# ---------------------------------------------------------------------------
# aggregate_entries / select_payload
# ---------------------------------------------------------------------------


def _entry(full_name="o/r", alias=None, description=None, categories=()):
    return MinedEntry(
        full_name=full_name, alias=alias,
        description=description, categories=tuple(categories),
    )


def test_aggregate_counts_per_list_not_per_occurrence():
    per_list = [
        ("l1", [
            _entry(alias="Tool", categories=("Frameworks",)),
            _entry(alias="Tool", categories=("Frameworks",)),  # same list, again
        ]),
        ("l2", [_entry(alias="Tool", categories=("Frameworks",))]),
    ]
    agg = aggregate_entries(per_list)["o/r"]
    assert agg.n_lists == 2
    assert agg.aliases["Tool"] == 2
    assert agg.categories["Frameworks"] == 2


def test_select_payload_prefers_independent_phrasings():
    per_list = [
        ("l1", [_entry(description="Tensors and Dynamic neural networks")]),
        ("l2", [_entry(description="Tensors and Dynamic neural networks")]),
        ("l3", [_entry(description="A deep learning framework for Python")]),
    ]
    agg = aggregate_entries(per_list)["o/r"]
    description, _, _ = select_payload(
        agg, "r", "o/r", own_description="Tensors and Dynamic neural networks",
    )
    lines = description.split("\n")
    # The phrasing that differs from the repo's own description leads,
    # despite having fewer votes.
    assert lines[0] == "A deep learning framework for Python"
    assert len(lines) == 2


def test_select_payload_filters_generic_and_self_aliases():
    per_list = [
        ("l1", [
            _entry(alias="Source Code"),
            _entry(alias="Next.js"),
            _entry(alias="nextjs"),
        ]),
    ]
    agg = aggregate_entries(per_list)["o/r"]
    _, aliases, _ = select_payload(agg, "next.js", "vercel/next.js", None)
    # "Source Code" is plumbing; "Next.js"/"nextjs" normalise to the
    # repo's own name and add nothing over the indexed name tokens.
    assert aliases == []


def test_select_payload_caps_lists():
    per_list = [
        ("l" + str(i), [_entry(
            alias=f"Alias{i % 10}",         # repeats -> passes the quorum
            categories=(f"Category {i}",),
            description=f"Unique description number {i} with substance.",
        )])
        for i in range(40)
    ]
    agg = aggregate_entries(per_list)["o/r"]
    description, aliases, categories = select_payload(agg, "r", "o/r", None)
    assert len(aliases) == 8
    assert len(categories) == 24
    assert len(description.split("\n")) <= 8


def test_alias_quorum_and_shape_filters():
    per_list = [
        ("l1", [_entry(alias="Qdrant"), _entry(alias="Dear ImGui")]),
        ("l2", [_entry(alias="Dear ImGui"), _entry(alias="🦜️🔗 LangChain")]),
        ("l3", [_entry(alias="[code"), _entry(alias="@handle/thing")]),
        ("l4", [_entry(alias="Source ⭐ 311K")]),
    ]
    agg = aggregate_entries(per_list)["o/r"]
    _, aliases, _ = select_payload(agg, "imgui", "ocornut/imgui", None)
    # 4 lists -> quorum applies: single-vote "Qdrant" dies; malformed
    # shapes die regardless of votes.
    assert aliases == ["Dear ImGui"]


def test_description_url_and_pipe_tail_are_stripped():
    md = (
        "- [x](https://github.com/o/r) - Chat platform. "
        "Join us: https://discord.gg/abc | CC-BY-4.0\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.description == "Chat platform. Join us:"


def test_question_and_cjk_headings_are_not_categories():
    md = (
        "## Looking for more lists like this?\n"
        "### GitHub篇\n"
        "- [x](https://github.com/o/r) - A tool for things.\n"
    )
    (entry,) = parse_awesome_readme(md)
    assert entry.categories == ()
