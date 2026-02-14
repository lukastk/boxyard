# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for Parent-Child Relationships

# %%
#|default_exp unit.models.test_parents

# %%
#|export
import pytest
from pydantic import ValidationError

from boxyard._models import BoxMeta, BoxyardMeta


# ============================================================================
# Helper to create BoxMeta instances for testing
# ============================================================================

# %%
#|export
def _make_box(timestamp, subid, name, groups=None, parents=None):
    return BoxMeta(
        creation_timestamp_utc=timestamp,
        box_subid=subid,
        name=name,
        storage_location="default",
        creator_hostname="testhost",
        groups=groups or [],
        parents=parents or [],
    )


# ============================================================================
# Tests for BoxMeta parents field
# ============================================================================

# %%
#|export
class TestBoxMetaParents:

    def test_parents_default_empty(self):
        box = _make_box("20251122", "aaaaa", "mybox")
        assert box.parents == []

    def test_parents_backwards_compat(self):
        """BoxMeta without parents field uses empty list default."""
        box = BoxMeta(
            creation_timestamp_utc="20251122",
            box_subid="aaaaa",
            name="mybox",
            storage_location="default",
            creator_hostname="testhost",
            groups=[],
        )
        assert box.parents == []

    def test_parents_set_on_creation(self):
        box = _make_box("20251122", "aaaaa", "mybox", parents=["20251122_bbbbb"])
        assert box.parents == ["20251122_bbbbb"]

    def test_duplicate_parents_rejected(self):
        with pytest.raises(ValidationError, match="Parents must be unique"):
            _make_box("20251122", "aaaaa", "mybox", parents=["20251122_bbbbb", "20251122_bbbbb"])

    def test_self_parent_rejected(self):
        with pytest.raises(ValidationError, match="cannot be its own parent"):
            _make_box("20251122", "aaaaa", "mybox", parents=["20251122_aaaaa"])

    def test_parents_in_model_dump(self):
        box = _make_box("20251122", "aaaaa", "mybox", parents=["20251122_bbbbb"])
        dump = box.model_dump()
        assert dump["parents"] == ["20251122_bbbbb"]

    def test_parents_round_trip(self):
        box = _make_box("20251122", "aaaaa", "mybox", parents=["20251122_bbbbb", "20251122_ccccc"])
        dump = box.model_dump()
        restored = BoxMeta(**dump)
        assert restored.parents == ["20251122_bbbbb", "20251122_ccccc"]


# ============================================================================
# Tests for BoxyardMeta parent-child helpers
# ============================================================================

# %%
#|export
class TestBoxyardMetaParentChild:

    @pytest.fixture
    def simple_dag(self):
        """Create A -> B -> C hierarchy."""
        a = _make_box("20251122", "aaaaa", "box_a")
        b = _make_box("20251122", "bbbbb", "box_b", parents=[a.box_id])
        c = _make_box("20251122", "ccccc", "box_c", parents=[b.box_id])
        return BoxyardMeta(box_metas=[a, b, c]), a, b, c

    @pytest.fixture
    def diamond_dag(self):
        """Create diamond: A -> B, A -> C, B -> D, C -> D."""
        a = _make_box("20251122", "aaaaa", "box_a")
        b = _make_box("20251122", "bbbbb", "box_b", parents=[a.box_id])
        c = _make_box("20251122", "ccccc", "box_c", parents=[a.box_id])
        d = _make_box("20251122", "ddddd", "box_d", parents=[b.box_id, c.box_id])
        return BoxyardMeta(box_metas=[a, b, c, d]), a, b, c, d

    def test_children_of(self, simple_dag):
        meta, a, b, c = simple_dag
        children = meta.children_of(a.box_id)
        assert len(children) == 1
        assert children[0].box_id == b.box_id

    def test_children_of_leaf(self, simple_dag):
        meta, a, b, c = simple_dag
        assert meta.children_of(c.box_id) == []

    def test_descendants_of(self, simple_dag):
        meta, a, b, c = simple_dag
        descs = meta.descendants_of(a.box_id)
        desc_ids = {d.box_id for d in descs}
        assert desc_ids == {b.box_id, c.box_id}

    def test_descendants_of_no_duplicates_diamond(self, diamond_dag):
        meta, a, b, c, d = diamond_dag
        descs = meta.descendants_of(a.box_id)
        desc_ids = [dd.box_id for dd in descs]
        # D should appear only once even though reachable via both B and C
        assert desc_ids.count(d.box_id) == 1
        assert set(desc_ids) == {b.box_id, c.box_id, d.box_id}

    def test_ancestors_of(self, simple_dag):
        meta, a, b, c = simple_dag
        ancs = meta.ancestors_of(c.box_id)
        anc_ids = {an.box_id for an in ancs}
        assert anc_ids == {a.box_id, b.box_id}

    def test_ancestors_of_root(self, simple_dag):
        meta, a, b, c = simple_dag
        assert meta.ancestors_of(a.box_id) == []

    def test_ancestors_of_diamond(self, diamond_dag):
        meta, a, b, c, d = diamond_dag
        ancs = meta.ancestors_of(d.box_id)
        anc_ids = {an.box_id for an in ancs}
        assert anc_ids == {a.box_id, b.box_id, c.box_id}

    def test_roots(self, simple_dag):
        meta, a, b, c = simple_dag
        roots = meta.roots()
        assert len(roots) == 1
        assert roots[0].box_id == a.box_id

    def test_roots_diamond(self, diamond_dag):
        meta, a, b, c, d = diamond_dag
        roots = meta.roots()
        assert len(roots) == 1
        assert roots[0].box_id == a.box_id

    def test_leaves(self, simple_dag):
        meta, a, b, c = simple_dag
        leaves = meta.leaves()
        assert len(leaves) == 1
        assert leaves[0].box_id == c.box_id

    def test_leaves_diamond(self, diamond_dag):
        meta, a, b, c, d = diamond_dag
        leaves = meta.leaves()
        assert len(leaves) == 1
        assert leaves[0].box_id == d.box_id

    def test_would_create_cycle_direct(self, simple_dag):
        """B -> A would create cycle since A -> B exists."""
        meta, a, b, c = simple_dag
        assert meta.would_create_cycle(a.box_id, b.box_id) is True

    def test_would_create_cycle_indirect(self, simple_dag):
        """C -> A would create cycle since A -> B -> C."""
        meta, a, b, c = simple_dag
        assert meta.would_create_cycle(a.box_id, c.box_id) is True

    def test_would_create_cycle_self(self, simple_dag):
        meta, a, b, c = simple_dag
        assert meta.would_create_cycle(a.box_id, a.box_id) is True

    def test_would_not_create_cycle(self, simple_dag):
        """C -> A is a cycle, but A -> C is not (A already ancestor of C)."""
        meta, a, b, c = simple_dag
        # Adding C as parent of new box D is fine
        d = _make_box("20251122", "ddddd", "box_d")
        meta2 = BoxyardMeta(box_metas=[*meta.box_metas, d])
        assert meta2.would_create_cycle(d.box_id, c.box_id) is False

    def test_missing_parent_graceful(self):
        """Ancestors of a box whose parent doesn't exist locally."""
        box = _make_box("20251122", "aaaaa", "mybox", parents=["nonexistent_id"])
        meta = BoxyardMeta(box_metas=[box])
        # Should not raise, just return empty
        assert meta.ancestors_of(box.box_id) == []

    def test_multiple_roots(self):
        a = _make_box("20251122", "aaaaa", "root1")
        b = _make_box("20251122", "bbbbb", "root2")
        c = _make_box("20251122", "ccccc", "child", parents=[a.box_id])
        meta = BoxyardMeta(box_metas=[a, b, c])
        roots = meta.roots()
        root_ids = {r.box_id for r in roots}
        assert root_ids == {a.box_id, b.box_id}
