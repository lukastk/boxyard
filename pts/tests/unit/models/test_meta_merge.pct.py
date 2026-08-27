# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Merging Two Boxmetas That Both Moved
#
# A boxmeta both sides have edited is a dead end today: sync sees two records
# that disagree, cannot tell which fields moved on which side, and refuses.
# Forty-four boxes on macbook stopped propagating their groups for a day in
# August 2026 that way, and each one needed a human.
#
# The merge reads each side's intent as a DELTA against the base rather than as
# a value to choose between, which is what makes it different from picking a
# winner. The properties that matter:
#
# * **Additions from both sides survive.** This is the whole point.
# * **A removal beats an addition.** Deleting a group the other side still has
#   is the newer intent about that entry; the reverse rule would make a group
#   impossible to remove while any machine was behind.
# * **It converges.** Machine A merges its local against the remote and pushes;
#   B then merges its own against what A pushed. Neither sees the other's base,
#   so the merge has to reach the same answer regardless of who goes first, or
#   two machines push each other's boxmeta back and forth forever.
# * **Two sides changing the same SCALAR differently is a real conflict**, left
#   for a human — for `write_owner` it means two machines each believe they own
#   the box.

# %%
#|default_exp unit.models.test_meta_merge

# %%
#|export
import itertools

import pytest

from boxyard._models import BoxMeta, MetaMergeConflict, merge_box_metas

# %% [markdown]
# ## Building the three sides

# %%
#|export
def meta(groups=(), parents=(), write_owner=None, creator="host-a", unknown=None):
    return BoxMeta(
        creation_timestamp_utc="20260822_000000",
        box_subid="aaaaa",
        name="a-box",
        storage_location="remote",
        creator_hostname=creator,
        groups=list(groups),
        parents=list(parents),
        write_owner=write_owner,
        unknown_keys=dict(unknown or {}),
    )

# %% [markdown]
# ## Additions from both sides survive

# %%
#|export
def test_both_additions_survive():
    # The real case: macbook added `archived`, the remote gained `write_owner`.
    base = meta(groups=["work"])
    local = meta(groups=["work", "archived"])
    remote = meta(groups=["work"], write_owner="mymain")

    merged = merge_box_metas(base, local, remote)

    assert merged.groups == ["work", "archived"]
    assert merged.write_owner == "mymain"


def test_each_side_adding_a_different_group():
    base = meta(groups=["work"])
    local = meta(groups=["work", "from-local"])
    remote = meta(groups=["work", "from-remote"])

    merged = merge_box_metas(base, local, remote)

    # Additions land in SORTED order, not local-first: the order has to be a
    # function of the content alone, or the two machines disagree about the
    # bytes and push forever.
    assert merged.groups == ["work", "from-local", "from-remote"]


def test_the_same_addition_on_both_sides_appears_once():
    base = meta(groups=[])
    both = meta(groups=["archived"])

    assert merge_box_metas(base, both, both).groups == ["archived"]

# %% [markdown]
# ## A removal beats an addition

# %%
#|export
def test_a_removal_on_one_side_wins():
    base = meta(groups=["work", "stale"])
    local = meta(groups=["work", "stale", "new"])
    remote = meta(groups=["work"])  # `stale` deliberately removed

    merged = merge_box_metas(base, local, remote)

    # `stale` stays gone: the remote's deletion is the newer intent about that
    # entry, and the local side simply never saw it change. The opposite rule
    # would make a group impossible to remove while any machine was behind.
    assert merged.groups == ["work", "new"]


def test_removing_on_both_sides():
    base = meta(groups=["work", "gone"])
    side = meta(groups=["work"])

    assert merge_box_metas(base, side, side).groups == ["work"]

# %% [markdown]
# ## Parents merge by the same rules

# %%
#|export
def test_parents_merge_as_a_set():
    base = meta(parents=["20260101_aaaaa"])
    local = meta(parents=["20260101_aaaaa", "20260202_bbbbb"])
    remote = meta(parents=["20260101_aaaaa", "20260303_ccccc"])

    merged = merge_box_metas(base, local, remote)

    assert merged.parents == ["20260101_aaaaa", "20260202_bbbbb", "20260303_ccccc"]

# %% [markdown]
# ## Scalars: one side changed, or a real conflict

# %%
#|export
@pytest.mark.parametrize(
    "base_owner,local_owner,remote_owner,expected",
    [
        (None, "mymain", None, "mymain"),      # claimed locally
        (None, None, "mymain", "mymain"),      # claimed elsewhere
        ("mymain", None, "mymain", None),      # released locally
        ("mymain", "mymain", None, None),      # released elsewhere
        ("mymain", "mymain", "mymain", "mymain"),
        (None, "mymain", "mymain", "mymain"),  # both claimed the SAME machine
    ],
)
def test_write_owner_when_only_one_side_moved(base_owner, local_owner, remote_owner, expected):
    merged = merge_box_metas(
        meta(write_owner=base_owner),
        meta(write_owner=local_owner),
        meta(write_owner=remote_owner),
    )
    assert merged.write_owner == expected


def test_two_different_claims_are_a_conflict():
    with pytest.raises(MetaMergeConflict) as excinfo:
        merge_box_metas(
            meta(write_owner=None),
            meta(write_owner="macbook"),
            meta(write_owner="mymain"),
        )
    # Named, so the refusal can say which field a human has to settle. Guessing
    # here would hand a box to a machine that does not think it has it.
    assert excinfo.value.fields == ["write_owner"]


def test_every_conflicting_field_is_named():
    with pytest.raises(MetaMergeConflict) as excinfo:
        merge_box_metas(
            meta(write_owner=None, creator="base"),
            meta(write_owner="macbook", creator="local"),
            meta(write_owner="mymain", creator="remote"),
        )
    assert excinfo.value.fields == ["creator_hostname", "write_owner"]

# %% [markdown]
# ## Keys from a newer boxyard are carried, not dropped

# %%
#|export
def test_unknown_keys_merge_per_key():
    base = meta(unknown={"shared": 1})
    local = meta(unknown={"shared": 1, "from_local": "x"})
    remote = meta(unknown={"shared": 1, "from_remote": "y"})

    merged = merge_box_metas(base, local, remote)

    # Dropping either would silently discard a field written by a boxyard
    # newer than this one -- exactly what `unknown_keys` exists to prevent.
    assert merged.unknown_keys == {"shared": 1, "from_local": "x", "from_remote": "y"}


def test_a_key_removed_on_one_side_goes():
    base = meta(unknown={"doomed": 1})
    local = meta(unknown={"doomed": 1})
    remote = meta(unknown={})

    assert merge_box_metas(base, local, remote).unknown_keys == {}


def test_conflicting_unknown_keys_are_a_conflict():
    with pytest.raises(MetaMergeConflict) as excinfo:
        merge_box_metas(
            meta(unknown={"k": 1}), meta(unknown={"k": 2}), meta(unknown={"k": 3})
        )
    assert excinfo.value.fields == ["unknown_keys.k"]

# %% [markdown]
# ## Boxmetas describing different boxes are a bug, not an edit

# %%
#|export
def test_mismatched_identity_raises():
    base = meta()
    other = base.model_copy(update={"name": "a-different-box"})
    with pytest.raises(ValueError, match="different boxes"):
        merge_box_metas(base, other, base)

# %% [markdown]
# ## Convergence
#
# The property no single-case test can express. Two machines merge
# independently, never seeing each other's base; if the merge did not converge
# they would push each other's boxmeta back and forth forever.

# %%
#|export
GROUP_UNIVERSE = ["a", "b", "c", "d"]


def _subsets(universe):
    for size in range(len(universe) + 1):
        for combo in itertools.combinations(universe, size):
            yield list(combo)


def test_the_merge_converges_over_every_group_triple():
    """Every (base, A, B) over a four-group universe: 16^3 = 4096 cases.

    A merges its local against the remote and pushes; B then merges its own
    local against what A pushed. The result must not depend on who pushed
    first, and it must be a FIXED POINT -- merging again changes nothing.

    Compared as LISTS, not as sets. The first version of this test used
    `sorted()` on both sides and passed while the merge was ordering additions
    local-first, which put 80 of these cases in a different order on each
    machine: same set, different bytes, so each side read the other's push as a
    change and pushed back. Two machines would have traded the same boxmeta
    every 20 minutes forever, and the assertion that was supposed to catch it
    was the thing hiding it.
    """
    checked = 0
    for base_g, a_g, b_g in itertools.product(_subsets(GROUP_UNIVERSE), repeat=3):
        base, a, b = meta(base_g), meta(a_g), meta(b_g)

        # A merges and pushes; B then merges against what A pushed.
        a_first = merge_box_metas(base, a, b)
        b_after_a = merge_box_metas(base, b, a_first)

        # The other order.
        b_first = merge_box_metas(base, b, a)
        a_after_b = merge_box_metas(base, a, b_first)

        assert b_after_a.groups == a_after_b.groups, (
            f"who merged first changed the ORDER: base={base_g} a={a_g} b={b_g} "
            f"-> {b_after_a.groups} vs {a_after_b.groups}"
        )

        # A settled state stays settled: A merging again against what B pushed
        # must produce exactly what B pushed.
        assert merge_box_metas(base, a_first, b_after_a).groups == b_after_a.groups, (
            f"not a fixed point: base={base_g} a={a_g} b={b_g}"
        )
        checked += 1

    assert checked == 4096, checked
