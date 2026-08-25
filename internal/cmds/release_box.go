package cmds

import (
	"context"
	"fmt"
	"io"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// ReleaseBoxOptions mirrors the Python `release_box` signature.
type ReleaseBoxOptions struct {
	BoxIndexName string
	Verbose      bool
	Out          io.Writer
	// SkipSync writes the boxmeta but does not push it. ONLY for callers that
	// are already inside a sync of this box — a release that is not pushed is
	// invisible to every other machine.
	SkipSync bool
}

// ReleaseBox gives up this machine's write ownership of a box.
//
// Ported from pts/mod/cmds/16_release_box.pct.py.
func ReleaseBox(ctx context.Context, cfg *config.Config, s SyncStore, p syncengine.Perms, opts ReleaseBoxOptions) error {
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	cached, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}
	storageLocation := cached.StorageLocation

	// Bring META up to date before deciding anything.
	//
	// Both the decision below and the push that follows depend on this
	// machine's boxmeta being current. Without it the sequence fails in an
	// avoidable and confusing way: META is writable by EVERY machine (it has to
	// be, or ownership could never be transferred), so a non-owner that merely
	// pulled the box has written a fresh META sync record of its own. Editing a
	// stale local boxmeta and pushing then reads as CONFLICT — both sides moved
	// — and the command refuses for a reason that has nothing to do with
	// ownership.
	//
	// A genuine META conflict still fails here, which is right: a person typed
	// this command and is waiting for an answer.
	if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		Choices:      []enums.BoxPart{enums.PartMeta},
		Setting:      enums.SyncCareful,
	}); err != nil {
		return err
	}

	boxMeta, err := models.LoadBoxMeta(cfg, storageLocation, opts.BoxIndexName)
	if err != nil {
		return err
	}
	if boxMeta.WriteOwner == "" {
		printf("'%s' is already unowned; nothing to release.\n", opts.BoxIndexName)
		return nil
	}
	if boxMeta.WriteOwner != cfg.MachineName {
		machine := cfg.MachineName
		if machine == "" {
			machine = "unnamed"
		}
		return &ownership.RefusedError{Message: fmt.Sprintf(
			"Cannot release '%s': it is owned by '%s', not by this machine (%s).\n"+
				"Run `boxyard release -r '%s'` on '%s', or take the box over here with "+
				"`boxyard claim --steal -r '%s'`.",
			opts.BoxIndexName, boxMeta.WriteOwner, machine,
			opts.BoxIndexName, boxMeta.WriteOwner, opts.BoxIndexName)}
	}

	// Write, push, and VERIFY — rolling back if it did not land.
	//
	// A release that only happened locally is worse than no release: every
	// other machine still believes this one owns the box, while this one
	// believes it is free. `exclude` depends on exactly this — it refuses to
	// drop a box it owns when the release cannot be published, and that refusal
	// is only honest if the local boxmeta is unchanged afterwards.
	previousOwner := boxMeta.WriteOwner
	restore := func() {
		boxMeta.WriteOwner = previousOwner
		if err := boxMeta.Save(cfg); err != nil {
			fmt.Fprintf(errOutOr(opts.Out), "Warning: could not restore the write owner of '%s': %v\n",
				opts.BoxIndexName, err)
		}
	}

	boxMeta.WriteOwner = ""
	if err := boxMeta.Save(cfg); err != nil {
		return err
	}

	if !opts.SkipSync {
		if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
			BoxIndexName: opts.BoxIndexName,
			Choices:      []enums.BoxPart{enums.PartMeta},
			Setting:      enums.SyncCareful,
		}); err != nil {
			restore()
			return err
		}
		landed, err := remoteOwnerIsCleared(ctx, cfg, s, boxMeta, opts.BoxIndexName)
		if err != nil {
			restore()
			return err
		}
		if !landed {
			restore()
			return &ownership.RefusedError{Message: fmt.Sprintf(
				"Could not publish the release of '%s': the remote boxmeta does not show it "+
					"as unowned, so other machines would still treat '%s' as the write owner. "+
					"This machine still owns it; try again once the remote is reachable.",
				opts.BoxIndexName, previousOwner)}
		}
	}

	if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
		return err
	}
	printf("Released '%s'. It is unowned again, so any machine may push it — "+
		"claim it elsewhere with `boxyard claim -r '%s'`.\n",
		opts.BoxIndexName, opts.BoxIndexName)
	return nil
}

// remoteOwnerIsCleared asks whether the REMOTE boxmeta now shows the box as
// unowned. Reading it back is the whole point: a push that reported success but
// did not land would otherwise leave the fleet and this machine disagreeing
// about who may write.
func remoteOwnerIsCleared(ctx context.Context, cfg *config.Config, s SyncStore, boxMeta *models.BoxMeta, boxIndexName string) (bool, error) {
	owner, exists, err := remoteWriteOwner(ctx, cfg, s, boxMeta, boxIndexName)
	if err != nil {
		return false, err
	}
	if !exists {
		return false, nil
	}
	return owner == "", nil
}
