# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for BoxyardFast

# %%
#|default_exp unit.test_fast

# %%
#|export
import pytest
import json
import inspect
from pathlib import Path

from boxyard._fast import BoxyardFast


# ============================================================================
# Fixtures
# ============================================================================

# %%
#|export
def _make_meta_dict(timestamp, subid, name, groups=None, parents=None):
    return {
        "creation_timestamp_utc": timestamp,
        "box_subid": subid,
        "name": name,
        "storage_location": "default",
        "creator_hostname": "testhost",
        "groups": groups or [],
        "parents": parents or [],
    }


@pytest.fixture
def simple_data():
    """A -> B -> C chain."""
    a = _make_meta_dict("20251122", "aaaaa", "box_a")
    b = _make_meta_dict("20251122", "bbbbb", "box_b", parents=["20251122_aaaaa"])
    c = _make_meta_dict("20251122", "ccccc", "box_c", parents=["20251122_bbbbb"])
    return {"box_metas": [a, b, c]}


@pytest.fixture
def diamond_data():
    """A -> B, A -> C, B -> D, C -> D."""
    a = _make_meta_dict("20251122", "aaaaa", "box_a", groups=["g1"])
    b = _make_meta_dict("20251122", "bbbbb", "box_b", groups=["g1"], parents=["20251122_aaaaa"])
    c = _make_meta_dict("20251122", "ccccc", "box_c", groups=["g2"], parents=["20251122_aaaaa"])
    d = _make_meta_dict("20251122", "ddddd", "box_d", groups=["g1", "g2"], parents=["20251122_bbbbb", "20251122_ccccc"])
    return {"box_metas": [a, b, c, d]}


# ============================================================================
# Tests: No boxyard imports in module source
# ============================================================================

# %%
#|export
class TestNoBoxyardImports:

    def test_no_boxyard_imports_in_source(self):
        """Verify _fast module has no boxyard imports."""
        source = inspect.getsource(BoxyardFast)
        assert "from boxyard" not in source
        assert "import boxyard" not in source


# ============================================================================
# Tests: from_file loading
# ============================================================================

# %%
#|export
class TestFromFile:

    def test_from_file(self, tmp_path, simple_data):
        meta_path = tmp_path / "boxyard_meta.json"
        meta_path.write_text(json.dumps(simple_data))
        fast = BoxyardFast.from_file(meta_path)
        assert len(fast._boxes) == 3

    def test_backwards_compat_no_parents(self, tmp_path):
        """JSON without parents key still works."""
        data = {"box_metas": [
            {"creation_timestamp_utc": "20251122", "box_subid": "aaaaa", "name": "old_box",
             "storage_location": "default", "creator_hostname": "host", "groups": ["g1"]},
        ]}
        meta_path = tmp_path / "boxyard_meta.json"
        meta_path.write_text(json.dumps(data))
        fast = BoxyardFast.from_file(meta_path)
        assert len(fast.roots()) == 1
        assert fast.roots()[0]["parents"] == []


# ============================================================================
# Tests: Parent-child methods
# ============================================================================

# %%
#|export
class TestParentChildMethods:

    def test_children_of(self, simple_data):
        fast = BoxyardFast(simple_data)
        children = fast.children_of("20251122_aaaaa")
        assert len(children) == 1
        assert children[0]["box_id"] == "20251122_bbbbb"

    def test_descendants_of(self, simple_data):
        fast = BoxyardFast(simple_data)
        descs = fast.descendants_of("20251122_aaaaa")
        desc_ids = {d["box_id"] for d in descs}
        assert desc_ids == {"20251122_bbbbb", "20251122_ccccc"}

    def test_parents_of(self, simple_data):
        fast = BoxyardFast(simple_data)
        parents = fast.parents_of("20251122_bbbbb")
        assert len(parents) == 1
        assert parents[0]["box_id"] == "20251122_aaaaa"

    def test_ancestors_of(self, simple_data):
        fast = BoxyardFast(simple_data)
        ancs = fast.ancestors_of("20251122_ccccc")
        anc_ids = {a["box_id"] for a in ancs}
        assert anc_ids == {"20251122_aaaaa", "20251122_bbbbb"}

    def test_roots(self, simple_data):
        fast = BoxyardFast(simple_data)
        roots = fast.roots()
        assert len(roots) == 1
        assert roots[0]["box_id"] == "20251122_aaaaa"

    def test_leaves(self, simple_data):
        fast = BoxyardFast(simple_data)
        leaves = fast.leaves()
        assert len(leaves) == 1
        assert leaves[0]["box_id"] == "20251122_ccccc"

    def test_is_ancestor(self, simple_data):
        fast = BoxyardFast(simple_data)
        assert fast.is_ancestor("20251122_ccccc", "20251122_aaaaa") is True
        assert fast.is_ancestor("20251122_aaaaa", "20251122_ccccc") is False

    def test_is_descendant(self, simple_data):
        fast = BoxyardFast(simple_data)
        assert fast.is_descendant("20251122_aaaaa", "20251122_ccccc") is True
        assert fast.is_descendant("20251122_ccccc", "20251122_aaaaa") is False

    def test_has_cycle_no_cycle(self, simple_data):
        fast = BoxyardFast(simple_data)
        assert fast.has_cycle() is False

    def test_has_cycle_with_cycle(self):
        data = {"box_metas": [
            _make_meta_dict("20251122", "aaaaa", "a", parents=["20251122_bbbbb"]),
            _make_meta_dict("20251122", "bbbbb", "b", parents=["20251122_aaaaa"]),
        ]}
        fast = BoxyardFast(data)
        assert fast.has_cycle() is True

    def test_would_create_cycle(self, simple_data):
        fast = BoxyardFast(simple_data)
        assert fast.would_create_cycle("20251122_aaaaa", "20251122_ccccc") is True
        assert fast.would_create_cycle("20251122_aaaaa", "20251122_aaaaa") is True

    def test_would_not_create_cycle(self, simple_data):
        fast = BoxyardFast(simple_data)
        # Adding aaaaa as parent of ccccc is fine (already is ancestor)
        # But adding a new unrelated box as parent is fine
        assert fast.would_create_cycle("20251122_ccccc", "20251122_aaaaa") is False

    def test_diamond_no_duplicates(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        descs = fast.descendants_of("20251122_aaaaa")
        desc_ids = [d["box_id"] for d in descs]
        assert desc_ids.count("20251122_ddddd") == 1
        assert set(desc_ids) == {"20251122_bbbbb", "20251122_ccccc", "20251122_ddddd"}


# ============================================================================
# Tests: Group filter parameter
# ============================================================================

# %%
#|export
class TestGroupFilter:

    def test_children_of_with_group_filter(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        children = fast.children_of("20251122_aaaaa", groups={"g1"})
        assert len(children) == 1
        assert children[0]["box_id"] == "20251122_bbbbb"

    def test_roots_with_group_filter(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        roots = fast.roots(groups={"g2"})
        # C is in g2 and has a parent (A), so it's not a root
        # But A is a root and not in g2, so with filter only C appears... but C has a parent
        # Actually roots() returns boxes with parents==[], which is only A.
        # A is not in g2, so filtered result is empty
        assert len(roots) == 0

    def test_descendants_with_group_filter(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        descs = fast.descendants_of("20251122_aaaaa", groups={"g2"})
        desc_ids = {d["box_id"] for d in descs}
        assert desc_ids == {"20251122_ccccc", "20251122_ddddd"}


# ============================================================================
# Tests: Group queries
# ============================================================================

# %%
#|export
class TestGroupQueries:

    def test_groups_of(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        assert fast.groups_of("20251122_ddddd") == ["g1", "g2"]

    def test_boxes_by_group(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        g1_boxes = fast.boxes_by_group("g1")
        g1_ids = {b["box_id"] for b in g1_boxes}
        assert g1_ids == {"20251122_aaaaa", "20251122_bbbbb", "20251122_ddddd"}

    def test_all_groups(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        assert fast.all_groups() == ["g1", "g2"]

    def test_all_boxes_with_groups(self, diamond_data):
        fast = BoxyardFast(diamond_data)
        result = fast.all_boxes_with_groups()
        assert "20251122_aaaaa__box_a" in result
        assert result["20251122_ddddd__box_d"] == ["g1", "g2"]


# ============================================================================
# Tests: DAG representation
# ============================================================================

# %%
#|export
class TestDAG:

    def test_get_dag_structure(self, simple_data):
        fast = BoxyardFast(simple_data)
        dag = fast.get_dag()
        assert set(dag.keys()) == {"20251122_aaaaa", "20251122_bbbbb", "20251122_ccccc"}
        assert dag["20251122_aaaaa"]["children"] == ["20251122_bbbbb"]
        assert dag["20251122_bbbbb"]["parents"] == ["20251122_aaaaa"]

    def test_get_dag_nested_from_roots(self, simple_data):
        fast = BoxyardFast(simple_data)
        nested = fast.get_dag_nested()
        assert "20251122_aaaaa" in nested
        root = nested["20251122_aaaaa"]
        assert root["name"] == "box_a"
        assert "20251122_bbbbb" in root["children"]
        child_b = root["children"]["20251122_bbbbb"]
        assert "20251122_ccccc" in child_b["children"]

    def test_get_dag_nested_from_specific_root(self, simple_data):
        fast = BoxyardFast(simple_data)
        nested = fast.get_dag_nested(root_id="20251122_bbbbb")
        assert "20251122_bbbbb" in nested
        assert "20251122_aaaaa" not in nested
