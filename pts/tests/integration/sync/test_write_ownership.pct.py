# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Write-ownership Integration Tests
#
# A box may be included on any number of machines, but exactly one machine at a
# time may PUSH its DATA. These tests drive two real boxyards sharing one remote,
# because every interesting property here is a property of two machines
# disagreeing — a single-boxyard test could not distinguish "refused" from
# "nothing to do".
#
# The properties that matter most, and are easiest to break silently:
#
# - an **unowned** box behaves exactly as it did before this feature existed;
# - a non-owner **pulls quietly** and never raises, because raising would
#   manufacture ~72 identical supervisor errors per machine per day;
# - **excluded debris** (`.DS_Store`) does not count as a local change, or every
#   read-only machine would report changes that do not exist, forever;
# - **`--sync-setting force` does not bypass ownership**, and neither does
#   `force-push`.

# %%
#|default_exp integration.sync.test_write_ownership

# %%
#|export
import asyncio
import tomllib

import pytest

from boxyard import const
from boxyard._models import BoxMeta, BoxPart, SyncCondition, get_boxyard_meta
from boxyard._ownership import OwnershipRefused
from boxyard._utils.sync_helper import SyncDirection, SyncSetting
from boxyard.cmds import (
    claim_box,
    discard_local,
    exclude_box,
    force_push_to_remote,
    include_box,
    new_box,
    release_box,
    sync_box,
    sync_missing_boxmetas,
)
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards


def run(coro):
    return asyncio.run(coro)


class Fleet:
    """Two boxyards sharing one remote, plus the box under test."""

    def __init__(
        self,
        include_on_m2: bool = False,
        claim_on_m1: bool = True,
        extra_exclude: str | None = None,
    ):
        (
            self.sl,
            self.remote_root,
            [(self.c1, self.cp1, _), (self.c2, self.cp2, _)],
        ) = create_boxyards(num_boxyards=2)

        # `claim=False`: this fixture's `claim_on_m1` knob is what decides
        # ownership, and from v0.5.17 `new_box` claims by default — which
        # would defeat the knob and make every `claim_on_m1=False` case start
        # owned. The tests below still assert the right property (an unclaimed
        # box behaves as it did in v0.4.x); they just need the state to be
        # reachable.
        self.index_name = new_box(
            config_path=self.cp1,
            box_name="shared",
            storage_location=self.sl,
            claim=False,
        )
        if extra_exclude is not None:
            # Appended to each machine's DEFAULT exclude file, which is the
            # per-machine config `sync_box` falls back to when a box has no
            # `conf/.rclone_exclude` of its own.
            #
            # Not the box's own conf/, deliberately: a box's conf never reaches
            # a machine that does not already have one (it reports `excluded`
            # forever), which is a pre-existing gap unrelated to ownership. A
            # fixture built on it would be testing that bug rather than this
            # code.
            for cfg_path in (self.cp1, self.cp2):
                default_exclude = get_config(cfg_path).default_rclone_exclude_path
                default_exclude.write_text(
                    default_exclude.read_text() + "\n" + extra_exclude
                )
        run(sync_box(config_path=self.cp1, box_index_name=self.index_name))
        run(sync_missing_boxmetas(config_path=self.cp2))
        if claim_on_m1:
            run(
                claim_box(
                    config_path=self.cp1,
                    box_index_name=self.index_name,
                    verbose=False,
                )
            )
        if include_on_m2:
            run(include_box(config_path=self.cp2, box_index_name=self.index_name))

    def meta(self, config) -> BoxMeta:
        """The box's boxmeta as it is ON DISK for that machine, not as cached."""
        return BoxMeta.load(get_config(config), self.sl, self.index_name)

    def data_path(self, config):
        return self.meta(config).get_local_part_path(get_config(config), BoxPart.DATA)

    @property
    def remote_data(self):
        return (
            self.remote_root
            / "boxyard"
            / const.REMOTE_BOXES_REL_PATH
            / self.index_name
            / const.BOX_DATA_REL_PATH
        )

    @property
    def remote_owner(self):
        raw = (
            self.remote_root
            / "boxyard"
            / const.REMOTE_BOXES_REL_PATH
            / self.index_name
            / const.BOX_METAFILE_REL_PATH
        ).read_text()
        return tomllib.loads(raw).get("write_owner")

    def sync_m2(self, **kwargs):
        return run(
            sync_box(
                config_path=self.cp2,
                box_index_name=self.index_name,
                verbose=False,
                **kwargs,
            )
        )


# ============================================================================
# An unowned box is exactly what it was before
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_unowned_box_is_unrestricted():
    """
    The regression guard for everybody else's boxes. Ownership is opt-in per
    box: anything else would mass-assign state to hundreds of boxes nobody
    chose, so a box that was never claimed must behave as it did in v0.4.x —
    both machines may push it.
    """
    fleet = Fleet(claim_on_m1=False, include_on_m2=True)
    assert fleet.meta(fleet.cp2).write_owner is None

    (fleet.data_path(fleet.cp2) / "from-m2.txt").write_text("m2 wrote this")
    results = fleet.sync_m2()

    assert results[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED
    assert (fleet.remote_data / "from-m2.txt").exists()


# ============================================================================
# A non-owner reads, and says nothing while it has nothing to say
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_non_owner_with_a_clean_box_syncs_quietly():
    """
    Must NOT raise. `multi-sync` runs every 1200s under supervisor and catches
    per-box exceptions into a red `Error` line, so an exception here would
    become ~72 identical unresolvable errors per machine per day.
    """
    fleet = Fleet(include_on_m2=True)
    results = fleet.sync_m2()
    assert results[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED


# %%
#|export
@pytest.mark.integration
def test_non_owner_pulls_the_owners_changes():
    fleet = Fleet(include_on_m2=True)

    (fleet.data_path(fleet.cp1) / "owner-work.txt").write_text("from the owner")
    run(sync_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))

    fleet.sync_m2()
    assert (fleet.data_path(fleet.cp2) / "owner-work.txt").read_text() == "from the owner"


# %%
#|export
@pytest.mark.integration
def test_excluded_debris_does_not_look_like_a_local_change():
    """
    The measured failure this probe exists to prevent: `.DS_Store` is in
    `DEFAULT_RCLONE_EXCLUDE` and can never be transferred, but it still flips a
    box to `needs_push`, because `get_sync_status` asks a tree walk. Without the
    probe every read-only machine would report "you have local changes" forever,
    for changes that do not exist, and the feature would be unusable.
    """
    fleet = Fleet(include_on_m2=True)

    (fleet.data_path(fleet.cp2) / ".DS_Store").write_text("junk")
    (fleet.data_path(fleet.cp2) / "__pycache__").mkdir(exist_ok=True)
    (fleet.data_path(fleet.cp2) / "__pycache__" / "x.pyc").write_text("junk")

    results = fleet.sync_m2()
    assert results[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED


# ============================================================================
# ...and speaks up when its work is stranded
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_non_owner_with_real_changes_is_write_denied():
    fleet = Fleet(include_on_m2=True)

    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("real work")
    results = fleet.sync_m2()

    assert results[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED
    assert results[BoxPart.DATA][1] is False
    # The point of the whole feature: the owner's remote is untouched.
    assert not (fleet.remote_data / "m2-work.txt").exists()


# %%
#|export
@pytest.mark.integration
def test_write_denied_names_the_owner_and_both_ways_out():
    """A refusal with one escape is a refusal people work around."""
    from boxyard._ownership import write_denied_hint, write_denied_message

    fleet = Fleet(include_on_m2=True)
    config, box_meta = get_config(fleet.cp2), fleet.meta(fleet.cp2)

    assert "test-machine-1" in write_denied_message(config, box_meta)
    hint = write_denied_hint(config, box_meta)
    assert "claim --steal" in hint
    assert "discard-local" in hint
    assert str(config.local_sync_backups_path) in hint


# %%
#|export
@pytest.mark.integration
def test_write_denied_is_not_an_error_for_multi_sync():
    """
    `multi-sync` decides Error/Read-only from the sync CONDITION, so this pins
    the contract it reads: a refused part is a status, not an exception.
    """
    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("real work")

    # Not raising IS the assertion; a raise would become a red `Error` line.
    results = fleet.sync_m2()

    assert any(
        status.sync_condition == SyncCondition.WRITE_DENIED
        for status, _ in results.values()
    )


# ============================================================================
# force does not bypass ownership
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_sync_setting_force_does_not_bypass_ownership():
    """
    `force` is a sync-SAFETY override ("I accept overwriting"); ownership is a
    COORDINATION statement. A forced push that left the remote holding this
    machine's data while boxmeta still named another owner would put a lie in
    shared state, which is worse than a refusal.
    """
    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / "forced.txt").write_text("forced")

    results = fleet.sync_m2(
        sync_setting=SyncSetting.FORCE, sync_direction=SyncDirection.PUSH
    )

    assert results[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED
    assert not (fleet.remote_data / "forced.txt").exists()


# %%
#|export
@pytest.mark.integration
def test_force_push_does_not_bypass_ownership():
    """`force-push` bypasses `sync_helper` entirely, so it carries its own gate."""
    fleet = Fleet(include_on_m2=True)
    source = fleet.data_path(fleet.cp2)
    (source / "forced.txt").write_text("forced")

    with pytest.raises(OwnershipRefused, match="test-machine-1"):
        run(
            force_push_to_remote(
                config_path=fleet.cp2,
                box_index_name=fleet.index_name,
                source_path=source,
                force=True,
            )
        )
    assert not (fleet.remote_data / "forced.txt").exists()


# ============================================================================
# claim / release
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_claim_on_an_already_owned_box_refuses_and_names_the_owner():
    fleet = Fleet(include_on_m2=True)

    with pytest.raises(OwnershipRefused) as excinfo:
        run(claim_box(config_path=fleet.cp2, box_index_name=fleet.index_name))

    message = str(excinfo.value)
    assert "test-machine-1" in message
    assert "--steal" in message
    assert fleet.remote_owner == "test-machine-1"


# %%
#|export
@pytest.mark.integration
def test_steal_takes_the_box_and_the_previous_owner_is_then_denied():
    fleet = Fleet(include_on_m2=True)

    run(claim_box(config_path=fleet.cp2, box_index_name=fleet.index_name, steal=True,
                  verbose=False))
    assert fleet.remote_owner == "test-machine-2"

    # The machine that used to own it now has its work refused, which is exactly
    # what `--steal` warns about before doing it.
    (fleet.data_path(fleet.cp1) / "m1-work.txt").write_text("stranded")
    results = run(
        sync_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False)
    )
    assert results[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED


# %%
#|export
@pytest.mark.integration
def test_release_then_claim_elsewhere_is_a_clean_handover():
    """The tidy path: two online steps, no force, no race."""
    fleet = Fleet(include_on_m2=True)

    run(release_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))
    assert fleet.remote_owner is None

    run(claim_box(config_path=fleet.cp2, box_index_name=fleet.index_name, verbose=False))
    assert fleet.remote_owner == "test-machine-2"

    # m2 can now push, and m1 cannot.
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("now allowed")
    results = fleet.sync_m2()
    assert results[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED
    assert (fleet.remote_data / "m2-work.txt").exists()


# %%
#|export
@pytest.mark.integration
def test_release_returns_the_boxmeta_to_its_pre_v05_form():
    fleet = Fleet()
    run(release_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))

    local_raw = (
        get_config(fleet.cp1).local_store_path
        / fleet.sl
        / fleet.index_name
        / const.BOX_METAFILE_REL_PATH
    ).read_text()
    assert "write_owner" not in local_raw


# %%
#|export
@pytest.mark.integration
def test_release_refuses_a_box_owned_by_another_machine():
    fleet = Fleet(include_on_m2=True)

    with pytest.raises(OwnershipRefused, match="test-machine-1"):
        run(release_box(config_path=fleet.cp2, box_index_name=fleet.index_name))

    assert fleet.remote_owner == "test-machine-1"


# ============================================================================
# discard-local
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_discard_local_leaves_a_recoverable_backup():
    """
    A command whose entire job is to throw work away has to be able to say
    where it went, so it force-pulls with `delete_backup=False` and prints the
    directory.
    """
    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("about to be discarded")

    backups_path = run(
        discard_local(
            config_path=fleet.cp2, box_index_name=fleet.index_name, verbose=False
        )
    )

    assert not (fleet.data_path(fleet.cp2) / "m2-work.txt").exists()
    recovered = list(backups_path.rglob("m2-work.txt"))
    assert recovered, f"nothing under {backups_path}"
    assert recovered[0].read_text() == "about to be discarded"


# %%
#|export
@pytest.mark.integration
def test_discard_local_then_sync_is_quiet():
    """After discarding, the box stops being reported at all."""
    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("discard me")
    assert fleet.sync_m2()[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED

    run(discard_local(config_path=fleet.cp2, box_index_name=fleet.index_name,
                      verbose=False))

    assert fleet.sync_m2()[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED


# ============================================================================
# A1 — claim refuses a box this machine does not hold
# ============================================================================
#
# A non-included box still has a local registration and boxmeta — that is
# exactly what `sync-missing-meta` maintains for the hundreds of boxes a machine
# does not hold — so without this check the claim SUCCEEDS and makes this
# machine the write owner of DATA it does not have. Every machine that does
# have it is then locked out, with `--steal` the only way back.

# %%
#|export
@pytest.mark.integration
def test_claim_refuses_a_box_that_is_not_included_here():
    fleet = Fleet(claim_on_m1=False)

    # m2 knows the box exists and has its boxmeta, but not its DATA.
    assert fleet.index_name in get_boxyard_meta(get_config(fleet.cp2)).by_index_name
    assert not fleet.meta(fleet.cp2).check_included(get_config(fleet.cp2))

    with pytest.raises(OwnershipRefused) as excinfo:
        run(claim_box(config_path=fleet.cp2, box_index_name=fleet.index_name))

    message = str(excinfo.value)
    assert "not included on this machine" in message
    # The fix must be named as an exact command, not described.
    assert f"boxyard include -r '{fleet.index_name}'" in message
    # And nothing was written anywhere.
    assert fleet.meta(fleet.cp2).write_owner is None
    assert fleet.remote_owner is None


# %%
#|export
@pytest.mark.integration
def test_claim_on_an_included_box_still_works():
    """The other half of A1: the refusal must not have broken the normal path."""
    fleet = Fleet(claim_on_m1=False)

    run(claim_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))

    assert fleet.meta(fleet.cp1).write_owner == "test-machine-1"
    assert fleet.remote_owner == "test-machine-1"


# ============================================================================
# A2 — exclude releases ownership in the same operation
# ============================================================================
#
# `exclude` reads as local housekeeping, but on a box this machine owns it would
# leave `boxmeta.toml` naming a machine that no longer has the DATA: NO machine
# could push it, and the only escape would be `--steal` from elsewhere. A
# command that looks local would have frozen the box fleet-wide, silently.

# %%
#|export
@pytest.mark.integration
def test_exclude_on_an_owned_box_releases_ownership():
    fleet = Fleet()
    assert fleet.remote_owner == "test-machine-1"

    run(exclude_box(config_path=fleet.cp1, box_index_name=fleet.index_name))

    assert fleet.meta(fleet.cp1).write_owner is None
    # Released on the REMOTE too, or the rest of the fleet still sees an owner
    # that no longer has the box.
    assert fleet.remote_owner is None


# %%
#|export
@pytest.mark.integration
def test_exclude_with_an_unreachable_remote_refuses_and_does_not_exclude():
    """
    Excluding anyway would create the exact frozen state this exists to prevent,
    just by a different route. So it refuses — and because it refuses, the box
    must still be here and still be owned afterwards.
    """
    fleet = Fleet()
    config = get_config(fleet.cp1)

    # Break the remote by pointing the rclone alias at nothing.
    config.rclone_config_path.write_text(
        f"[{fleet.sl}]\ntype = alias\nremote = /nonexistent/definitely-not-here\n"
    )

    with pytest.raises(RuntimeError) as excinfo:
        run(exclude_box(config_path=fleet.cp1, box_index_name=fleet.index_name))

    message = str(excinfo.value)
    assert "write owner" in message
    assert f"boxyard release -r '{fleet.index_name}'" in message

    # Nothing happened: the box is still here, and still owned by this machine.
    assert fleet.meta(fleet.cp1).check_included(config)
    assert fleet.meta(fleet.cp1).write_owner == "test-machine-1"


# %%
#|export
@pytest.mark.integration
def test_exclude_of_a_box_owned_by_another_machine_is_unchanged():
    """This machine was never the writer, so there is nothing to release."""
    fleet = Fleet(include_on_m2=True)
    assert fleet.remote_owner == "test-machine-1"

    run(exclude_box(config_path=fleet.cp2, box_index_name=fleet.index_name))

    assert not fleet.meta(fleet.cp2).check_included(get_config(fleet.cp2))
    # The owner is untouched -- excluding a replica must not disturb the writer.
    assert fleet.remote_owner == "test-machine-1"


# ============================================================================
# A3 — stale-owner catches what A1 and A2 did not think of
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_stale_owner_reports_a_box_we_own_but_do_not_have():
    """
    The exact sub-case, reached by writing the state directly rather than
    through a command: A1 and A2 close the two routes we know of, and this
    check exists for the routes we do not.
    """
    from boxyard.cmds import run_doctor

    fleet = Fleet()
    config = get_config(fleet.cp1)

    # Excluding through the command would release ownership (A2), so put the
    # box into the bad state by hand -- which is what an unknown route would do.
    import shutil

    shutil.rmtree(fleet.data_path(fleet.cp1))

    report = run(run_doctor(config_path=fleet.cp1, check_remote=False))
    findings = report["checks"]["stale-owner"]["findings"]

    assert [f["index_name"] for f in findings] == [fleet.index_name]
    assert "not included here" in findings[0]["message"]
    assert f"boxyard release -r '{fleet.index_name}'" in findings[0]["hint"]


# %%
#|export
@pytest.mark.integration
def test_stale_owner_is_quiet_when_ownership_is_healthy():
    from boxyard.cmds import run_doctor

    fleet = Fleet()
    report = run(run_doctor(config_path=fleet.cp1, check_remote=False))
    assert not report["checks"]["stale-owner"]["findings"]

    # And a box legitimately owned by ANOTHER machine, not included here, is
    # the ordinary state of most boxes on most machines -- never a finding.
    report = run(run_doctor(config_path=fleet.cp2, check_remote=False))
    assert not report["checks"]["stale-owner"]["findings"]


# %%
#|export
@pytest.mark.integration
def test_write_denied_is_reported_by_doctor_with_both_ways_out():
    from boxyard.cmds import run_doctor

    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("stranded work")

    report = run(run_doctor(config_path=fleet.cp2))
    findings = report["checks"]["write-denied"]["findings"]

    assert [f["index_name"] for f in findings] == [fleet.index_name]
    assert "test-machine-1" in findings[0]["message"]
    assert "claim --steal" in findings[0]["hint"]
    assert "discard-local" in findings[0]["hint"]


# %%
#|export
@pytest.mark.integration
def test_write_denied_is_quiet_for_debris_only():
    """
    Doctor must apply the same probe the sync path does, or the two would
    disagree and the report would cry wolf about files that can never move.
    """
    from boxyard.cmds import run_doctor

    fleet = Fleet(include_on_m2=True)
    (fleet.data_path(fleet.cp2) / ".DS_Store").write_text("junk")

    report = run(run_doctor(config_path=fleet.cp2))
    assert not report["checks"]["write-denied"]["findings"]


# ============================================================================
# The branches the first pass of these tests did not actually reach
# ============================================================================
#
# Found by mutation-checking: breaking the probe, the CONFLICT arm and the
# read-backs left every test above still passing. Each test here exists because
# a specific branch had no coverage, and each is named for that branch.

# %%
#|export
@pytest.mark.integration
def test_the_probe_is_what_saves_a_glob_excluded_file():
    """
    The probe's real job, and the one `.DS_Store` does NOT demonstrate.

    v0.4.6 already stops LITERAL excludes (`.DS_Store`, `__pycache__/`) from
    looking like changes, because `literal_exclude_names` reads them straight
    out of the exclude file. It deliberately does not interpret GLOBS —
    reimplementing rclone's filter language would be a second, subtly different
    implementation of the thing that decides what actually transfers — so a
    glob-excluded file still flips the box to `needs_push`.

    On an ordinary machine that costs one no-op push. On a non-owner it would be
    a PERMANENT false `WRITE_DENIED`, reported forever, for a file that can
    never be transferred. The probe is what closes that gap.
    """
    fleet = Fleet(include_on_m2=True, extra_exclude="*.tmp\n")

    (fleet.data_path(fleet.cp2) / "scratch.tmp").write_text("glob-excluded junk")

    results = fleet.sync_m2()
    assert results[BoxPart.DATA][0].sync_condition != SyncCondition.WRITE_DENIED


# %%
#|export
@pytest.mark.integration
def test_doctor_is_quiet_about_a_glob_excluded_file_too():
    """Doctor runs the same probe, or it and sync would disagree."""
    from boxyard.cmds import run_doctor

    fleet = Fleet(include_on_m2=True, extra_exclude="*.tmp\n")
    (fleet.data_path(fleet.cp2) / "scratch.tmp").write_text("glob-excluded junk")

    report = run(run_doctor(config_path=fleet.cp2))
    assert not report["checks"]["write-denied"]["findings"]


# %%
#|export
@pytest.mark.integration
def test_a_non_owner_in_conflict_is_denied_not_raised():
    """
    The CONFLICT arm. It matters that this is a STATUS and not the `SyncUnsafe`
    exception `sync_helper` would otherwise raise: under the supervisor an
    exception becomes a red `Error` line every 1200s, forever, for a box whose
    state is understood.
    """
    fleet = Fleet(include_on_m2=True)

    # The owner moves on, so the records diverge...
    (fleet.data_path(fleet.cp1) / "owner-work.txt").write_text("owner moved on")
    run(sync_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))
    # ...while m2 changes its copy without pulling first.
    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("and so did m2")

    results = fleet.sync_m2()

    assert results[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED
    assert not (fleet.remote_data / "m2-work.txt").exists()


# %%
#|export
@pytest.mark.integration
def test_claim_fails_loudly_when_it_did_not_stick():
    """
    Concurrent claims are last-write-wins — measured at 5 trials in 6 — and the
    LOSER reverts silently, because a completed push writes a fresh sync record
    so its own claim then reads as an ordinary `needs_pull`. A `claim` that
    printed "ok" and did not stick would be worse than no command at all.

    The race itself is not reproducible on demand, so the read-back is driven
    directly: this is the state the losing machine finds itself in.
    """
    import boxyard.cmds._claim_box as claim_module

    fleet = Fleet(claim_on_m1=False)

    async def _remote_says_someone_else_won(**kwargs):
        return True, 'storage_location = "x"\nwrite_owner = "test-machine-2"\n'

    original = claim_module.rclone_cat
    claim_module.rclone_cat = _remote_says_someone_else_won
    try:
        with pytest.raises(OwnershipRefused) as excinfo:
            run(claim_box(config_path=fleet.cp1, box_index_name=fleet.index_name))
    finally:
        claim_module.rclone_cat = original

    message = str(excinfo.value)
    assert "did not stick" in message
    assert "test-machine-2" in message
    assert "--steal" in message


# %%
#|export
@pytest.mark.integration
def test_release_rolls_back_when_the_push_did_not_land():
    """
    A release that only happened locally is worse than no release: every other
    machine still believes this one owns the box, while this one believes it is
    free. `exclude`'s refusal depends on this rollback being real.
    """
    import boxyard.cmds._release_box as release_module

    fleet = Fleet()
    assert fleet.meta(fleet.cp1).write_owner == "test-machine-1"

    async def _never_landed(*args, **kwargs):
        return False

    original = release_module._remote_owner_is_cleared
    release_module._remote_owner_is_cleared = _never_landed
    try:
        with pytest.raises(OwnershipRefused, match="Could not publish the release"):
            run(release_box(config_path=fleet.cp1, box_index_name=fleet.index_name))
    finally:
        release_module._remote_owner_is_cleared = original

    # The local boxmeta must be exactly as it was, or the next command would
    # act on a release that never happened.
    assert fleet.meta(fleet.cp1).write_owner == "test-machine-1"


# %%
#|export
@pytest.mark.integration
def test_data_only_sync_still_sees_a_fresh_claim():
    """
    `boxyard sync -c data` must not decide ownership from a stale local
    boxmeta. It is the one path by which a non-owner could push without ever
    learning the box had been claimed, so META is synced whenever DATA is,
    whether or not the caller asked for it.
    """
    fleet = Fleet(claim_on_m1=False, include_on_m2=True)

    # m1 claims AFTER m2 last looked, so m2's local boxmeta still says unowned.
    run(claim_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))
    assert fleet.meta(fleet.cp2).write_owner is None

    (fleet.data_path(fleet.cp2) / "m2-work.txt").write_text("racing the claim")
    results = fleet.sync_m2(sync_choices=[BoxPart.DATA])

    assert results[BoxPart.DATA][0].sync_condition == SyncCondition.WRITE_DENIED
    assert not (fleet.remote_data / "m2-work.txt").exists()
    # The caller asked for DATA only, so that is all the result reports...
    assert set(results) == {BoxPart.DATA}
    # ...but META was synced anyway, which is how the claim was seen.
    assert fleet.meta(fleet.cp2).write_owner == "test-machine-1"


# ============================================================================
# The migration pass
# ============================================================================
#
# `claim --all-included` is how ownership gets assigned across the yard in one
# go, on each machine in turn. It is worth testing carefully because it is run
# once, fleet-wide, against hundreds of boxes.

# %%
#|export
def _cli(config_path, *args):
    from typer.testing import CliRunner

    from boxyard._cli.main import app

    return CliRunner().invoke(app, ["--config", str(config_path), *args])


@pytest.mark.integration
def test_claim_all_included_claims_every_unowned_included_box():
    fleet = Fleet(claim_on_m1=False)
    second = new_box(
        config_path=fleet.cp1, box_name="also-mine", storage_location=fleet.sl, claim=False
    )
    run(sync_box(config_path=fleet.cp1, box_index_name=second, verbose=False))

    result = _cli(fleet.cp1, "claim", "--all-included")
    assert result.exit_code == 0, result.output

    config = get_config(fleet.cp1)
    assert BoxMeta.load(config, fleet.sl, fleet.index_name).write_owner == "test-machine-1"
    assert BoxMeta.load(config, fleet.sl, second).write_owner == "test-machine-1"


# %%
#|export
@pytest.mark.integration
def test_claim_all_included_skips_boxes_in_a_local_storage_location():
    """
    No other machine can reach a local storage location, so there is nothing to
    coordinate. Attempting them and reporting each refusal would bury the boxes
    that genuinely need a decision — and the fixture yard, like the real one,
    has a local location.
    """
    fleet = Fleet(claim_on_m1=False)
    local_box = new_box(
        config_path=fleet.cp1, box_name="local-only", storage_location="fake", claim=False
    )

    result = _cli(fleet.cp1, "claim", "--all-included")

    assert result.exit_code == 0, result.output
    assert local_box not in result.output


# %%
#|export
@pytest.mark.integration
def test_claim_all_included_reports_the_boxes_that_refuse_without_stopping():
    """
    A box genuinely included on two machines hits a refusal on the second one.
    That is the POINT of the bulk pass — it enumerates exactly the boxes that
    were at risk all along — so it must keep going and list them at the end
    rather than stopping at the first.
    """
    fleet = Fleet(include_on_m2=True)  # m1 owns the shared box, m2 has it too
    mine_alone = new_box(
        config_path=fleet.cp2, box_name="only-on-m2", storage_location=fleet.sl, claim=False
    )
    run(sync_box(config_path=fleet.cp2, box_index_name=mine_alone, verbose=False))

    result = _cli(fleet.cp2, "claim", "--all-included")

    # Exit 1 because something needs a decision...
    assert result.exit_code == 1, result.output
    # ...the double-included box is named, with its owner and the three ways
    # out, because enumerating these is the point of running a pass at all...
    assert fleet.index_name in result.output
    assert "test-machine-1" in result.output
    assert "--steal" in result.output
    assert "discard-local" in result.output
    assert "exclude" in result.output
    # ...it was NOT silently taken...
    assert fleet.remote_owner == "test-machine-1"
    # ...and the box that was claimable was still claimed.
    config = get_config(fleet.cp2)
    assert BoxMeta.load(config, fleet.sl, mine_alone).write_owner == "test-machine-2"


# %%
#|export
@pytest.mark.integration
def test_claim_all_included_refuses_to_be_combined_with_steal():
    """A bulk pass must never take boxes from other machines."""
    fleet = Fleet(include_on_m2=True)

    result = _cli(fleet.cp2, "claim", "--all-included", "--steal")

    assert result.exit_code == 1
    assert "must never take boxes from other machines" in result.output
    # The other machine still owns it.
    assert fleet.remote_owner == "test-machine-1"


# ============================================================================
# The probe-clean baseline must be readable next pass, for every part
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_conf_probe_clean_baseline_converges_instead_of_reprobing_forever():
    """
    When the probe proves a non-owner clean, the baseline it records must be
    written under the SAME filter signature the status check reads it with.
    CONF syncs with no exclude file, so that signature is
    `filter_signature(None)` -- a fallback to the machine's default exclude
    here (which this once did) makes the baseline write-only garbage: the box
    falls back to the mtime test, reads NEEDS_PUSH again, and pays a remote
    `rclone check` on every supervisor pass, for ever -- the exact
    non-convergence the baseline write exists to prevent.
    """
    import json
    import os

    from boxyard._fingerprint import base_path_for, filter_signature
    from boxyard.cmds import _sync_box as _sync_box_module

    fleet = Fleet(include_on_m2=True)

    # The owner gives the box a conf file; the non-owner pulls it.
    m1_conf = fleet.meta(fleet.cp1).get_local_part_path(
        get_config(fleet.cp1), BoxPart.CONF
    )
    m1_conf.mkdir(parents=True, exist_ok=True)
    (m1_conf / "keep.txt").write_text("conf content")
    run(sync_box(config_path=fleet.cp1, box_index_name=fleet.index_name, verbose=False))
    fleet.sync_m2()

    # An mtime-only touch: the fingerprint flips NEEDS_PUSH, but the probe
    # (content-based) finds nothing to transfer -- the probe-clean path.
    m2_conf = fleet.meta(fleet.cp2).get_local_part_path(
        get_config(fleet.cp2), BoxPart.CONF
    )
    os.utime(m2_conf / "keep.txt")
    fleet.sync_m2()

    # The baseline it just recorded is bound to the adopted record and carries
    # the reader's signature...
    conf_rec = fleet.meta(fleet.cp2).get_local_sync_record_path(
        get_config(fleet.cp2), BoxPart.CONF
    )
    base = json.loads(base_path_for(conf_rec).read_text())
    assert base["filter_signature"] == filter_signature(None)

    # ...so the next pass converges: no part asks the remote what a push
    # would transfer, because nothing reads NEEDS_PUSH any more.
    probe_calls = []
    real_probe = _sync_box_module.push_would_transfer

    async def _spy(*args, **kwargs):
        probe_calls.append(1)
        return await real_probe(*args, **kwargs)

    from unittest.mock import patch

    with patch.object(_sync_box_module, "push_would_transfer", new=_spy):
        results = fleet.sync_m2()

    assert not probe_calls
    assert results[BoxPart.CONF][0].sync_condition == SyncCondition.SYNCED
