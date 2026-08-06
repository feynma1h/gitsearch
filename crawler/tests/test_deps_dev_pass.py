"""Tests for the deps.dev pass's pure selection logic."""

from src.deps_dev_pass import (
    MAX_VERSION_SAMPLES,
    pick_default_version,
    sample_versions,
    select_packages,
)


def _pv(system, name, relation="SOURCE_REPO"):
    return {
        "versionKey": {"system": system, "name": name, "version": "1.0.0"},
        "relationType": relation,
    }


def test_select_packages_prefers_eponymous_then_short():
    versions = [
        _pv("MAVEN", "org.pytorch:pytorch_android_torchvision"),
        _pv("PYPI", "torch"),
        _pv("MAVEN", "org.pytorch:pytorch_java_only"),
        _pv("GO", "github.com/pytorch/pytorch"),
    ]
    picks = select_packages(versions, "pytorch", cap=2)
    # Both "torch" (suffix of the repo name) and the GO module's
    # "pytorch" tail count as eponymous; the shorter package name wins
    # the tie. Both beat the deep Maven artifacts.
    assert picks[0] == ("PYPI", "torch")
    assert picks[1] == ("GO", "github.com/pytorch/pytorch")


def test_select_packages_ignores_issue_tracker_links():
    versions = [
        _pv("NPM", "left-pad", relation="ISSUE_TRACKER"),
        _pv("NPM", "the-real-one"),
    ]
    assert select_packages(versions, "thing") == [("NPM", "the-real-one")]


def test_select_packages_dedupes_versions_of_one_package():
    versions = [_pv("PYPI", "torch"), _pv("PYPI", "torch")]
    assert select_packages(versions, "pytorch") == [("PYPI", "torch")]


def _package(names, default=None):
    versions = []
    for i, name in enumerate(names):
        versions.append({
            "versionKey": {"system": "PYPI", "name": "x", "version": name},
            "publishedAt": f"2026-01-{31 - i:02d}T00:00:00Z",
            "isDefault": name == default,
        })
    return {"versions": versions}


def test_pick_default_version():
    assert pick_default_version(_package(["2.0", "1.9"], default="2.0")) == "2.0"
    assert pick_default_version(_package(["2.0", "1.9"])) is None


def test_sample_versions_spreads_and_caps():
    names = [f"1.0.{i}" for i in range(100, 0, -1)]  # newest first by date
    package = _package(names, default="1.0.100")
    picks = sample_versions(package)
    assert len(picks) <= MAX_VERSION_SAMPLES
    assert picks[0] == "1.0.100"          # default leads
    assert "1.0.99" in picks or "1.0.98" in picks   # newest-biased
    # At least one pick from the older half (long-lived pins).
    older_half = set(names[len(names) // 2:])
    assert any(p in older_half for p in picks)


def test_sample_versions_handles_tiny_packages():
    assert sample_versions(_package(["0.1"], default="0.1")) == ["0.1"]
    assert sample_versions({"versions": []}) == []
