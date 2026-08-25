# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Picker Must Not Silently Act on the Wrong Box
#
# `run_fzf` and `run_fzf_multi` return the LINE the user chose and map it back
# to a term with `disp_terms.index(...)`, which returns the **first** match. Two
# items rendering to the same line therefore resolve to the same term — so
# picking the second one silently acts on the first.
#
# Every caller today embeds the box id in its display line, so this cannot fire
# in production. The guard exists so that a caller which forgets gets a loud
# error instead of the wrong box removed from the machine by `boxyard exclude`.

# %%
#|default_exp unit._utils.test_fzf_guard

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import pytest

from boxyard._utils.base import _reject_ambiguous_disp_terms, run_fzf, run_fzf_multi

# %% [markdown]
# ## Unique display terms are accepted

# %%
#|export
def test_unique_display_terms_are_fine():
    _reject_ambiguous_disp_terms(["a", "b"], ["x (1)", "x (2)"])

# %% [markdown]
# ## Duplicates are refused, and the message names the culprit

# %%
#|export
def test_duplicate_display_terms_are_refused():
    # TESTREF: test_fzf_rejects_duplicate_display_terms
    with pytest.raises(ValueError) as excinfo:
        _reject_ambiguous_disp_terms(["a", "b"], ["same", "same"])
    assert "same" in str(excinfo.value)


def test_duplicates_after_stripping_are_refused():
    """The lookup strips, so the check must strip too."""
    with pytest.raises(ValueError):
        _reject_ambiguous_disp_terms(["a", "b"], ["same", "  same  "])

# %% [markdown]
# ## Mismatched lengths are refused
#
# The mapping is positional, so a short `terms` would resolve a high index to
# the wrong item or raise an opaque IndexError deep inside the wrapper.

# %%
#|export
def test_mismatched_lengths_are_refused():
    with pytest.raises(ValueError):
        _reject_ambiguous_disp_terms(["a"], ["x", "y"])

# %% [markdown]
# ## Both wrappers apply the guard before ever launching fzf

# %%
#|export
def test_both_wrappers_refuse_before_launching_fzf(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("fzf was launched despite ambiguous display terms")

    monkeypatch.setattr("subprocess.run", _explode)

    with pytest.raises(ValueError):
        run_fzf(terms=["a", "b"], disp_terms=["same", "same"])
    with pytest.raises(ValueError):
        run_fzf_multi(terms=["a", "b"], disp_terms=["same", "same"])
