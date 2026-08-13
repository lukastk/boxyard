# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for `validate_box_name`
#
# A box name is used verbatim as a directory name, so it has to be a single
# path component. A name containing a separator spreads the box over a nested
# tree whose top level does not parse as a box registration.

# %%
#|default_exp unit.models.test_box_name_validation

# %%
#|export
import pytest

from boxyard._models import validate_box_name

# %%
#|export
@pytest.mark.parametrize(
    "name",
    [
        "my-box",
        "my_box",
        "lukastk.github.io",
        "box with spaces",
        "20260101-notes",
        "a",
        "name__with__underscores",
    ],
)
def test_validate_box_name_accepts_valid_names(name):
    validate_box_name(name)

# %%
#|export
@pytest.mark.parametrize(
    "name",
    [
        "/github.com/lukastk/lukastk.github.io",  # the name that broke a real yard
        "github.com/lukastk/lukastk.github.io",
        "nested/name",
        "trailing/",
        "back\\slash",
        "with\0null",
        "",
        ".",
        "..",
        ".hidden",
        " leading-space",
        "trailing-space ",
    ],
)
def test_validate_box_name_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        validate_box_name(name)

# %%
#|export
def test_validate_box_name_rejects_non_strings():
    with pytest.raises(ValueError):
        validate_box_name(None)
