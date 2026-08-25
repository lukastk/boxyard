package cmds

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"syscall"
	"time"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
)

// IncludeBoxOptions mirrors the Python `include_box` signature.
type IncludeBoxOptions struct {
	BoxIndexName string
	// ReadOnly suppresses the nudge to claim an unowned box. Use it when the
	// caller is including the box for reference rather than to work on it.
	ReadOnly bool
	Out      io.Writer
}

// IncludeBox checks a box out onto this machine.
//
// Ported from pts/mod/cmds/07_include_box.pct.py.
func IncludeBox(ctx context.Context, cfg *config.Config, s SyncStore, p PermsWithSync, opts IncludeBoxOptions) error {
	out := opts.Out
	if out == nil {
		out = io.Discard
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return fmt.Errorf("Box '%s' does not exist.", opts.BoxIndexName)
	}
	if bm.CheckIncluded(cfg) {
		return fmt.Errorf("Box '%s' is already included.", opts.BoxIndexName)
	}

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return err
	}
	defer release()

	// DATA is FORCE-PULLED. There is nothing local to lose — the box is not
	// checked out here — and CAREFUL would read the absence as EXCLUDED and
	// refuse, which is the state we are leaving.
	pull := enums.DirectionPull
	if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		Direction:    &pull,
		Setting:      enums.SyncForce,
		Choices:      []enums.BoxPart{enums.PartData},
		SkipLock:     true,
	}); err != nil {
		return err
	}
	// META and CONF go the ordinary careful way.
	if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		Setting:      enums.SyncCareful,
		Choices:      []enums.BoxPart{enums.PartMeta, enums.PartConf},
		SkipLock:     true,
	}); err != nil {
		return err
	}

	fmt.Fprintf(out, "Included box '%s'\n", bm.Name)

	// Say what including this box means for WRITING to it. Re-read from disk:
	// the syncs above may have pulled a boxmeta naming an owner this machine
	// did not know about a moment ago, which is the whole point of saying
	// anything here.
	included, err := models.LoadBoxMeta(cfg, bm.StorageLocation, opts.BoxIndexName)
	if err != nil {
		return err
	}
	switch {
	case included.WriteOwner == "":
		// Unowned means unrestricted, so nothing is being withheld — but a box
		// nobody has claimed is a box two machines can still diverge on, which
		// is the problem ownership exists to remove. One line, no ceremony.
		if !opts.ReadOnly {
			fmt.Fprintf(out, "'%s' has no write owner. If this machine is where you will "+
				"work on it, claim it: `boxyard claim -r '%s'`.\n",
				opts.BoxIndexName, opts.BoxIndexName)
		}
	case included.WriteOwner != cfg.MachineName:
		fmt.Fprintf(out, "Included read-only — '%s' is the write owner of '%s', so this copy "+
			"pulls but never pushes. To work on it here, take it over with "+
			"`boxyard claim --steal -r '%s'`.\n",
			included.WriteOwner, opts.BoxIndexName, opts.BoxIndexName)
	}
	return nil
}

// ExcludeBoxOptions mirrors the Python `exclude_box` signature.
type ExcludeBoxOptions struct {
	BoxIndexName string
	// SkipSync drops the local copy without pushing first. Only for callers
	// that have already synced.
	SkipSync bool
	Out      io.Writer
}

// ExcludeBox removes a box's DATA from this machine, keeping the remote.
//
// Ported from pts/mod/cmds/06_exclude_box.pct.py.
func ExcludeBox(ctx context.Context, cfg *config.Config, s SyncStore, p PermsWithSync, opts ExcludeBoxOptions) error {
	out := opts.Out
	if out == nil {
		out = io.Discard
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return fmt.Errorf("Box '%s' does not exist.", opts.BoxIndexName)
	}
	if !bm.CheckIncluded(cfg) {
		return fmt.Errorf("Box '%s' is already excluded.", opts.BoxIndexName)
	}
	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return err
	}
	if slConfig.StorageType == config.StorageLocal {
		// A local storage location IS the local copy: excluding would delete
		// the only copy there is.
		return fmt.Errorf("Box '%s' in local storage location '%s' cannot be excluded.",
			opts.BoxIndexName, bm.StorageLocation)
	}

	// Release write ownership FIRST, if this machine holds it.
	//
	// `exclude` reads as local housekeeping — "I do not need this box here any
	// more" — but on a box THIS machine owns it would leave boxmeta.toml naming
	// a machine that no longer has the DATA. NO machine could then push it, and
	// the only escape would be `claim --steal` from somewhere else. A command
	// that looks local would have frozen the box fleet-wide, silently.
	//
	// Release pushes META, which makes `exclude` a network operation. If that
	// push cannot happen we REFUSE rather than excluding and leaving a stale
	// owner behind: doing it anyway "because the network was down" would create
	// the exact state this prevents, by a different route.
	onDisk, err := models.LoadBoxMeta(cfg, bm.StorageLocation, opts.BoxIndexName)
	if err != nil {
		return err
	}
	if onDisk.WriteOwner != "" && onDisk.WriteOwner == cfg.MachineName {
		if err := ReleaseBox(ctx, cfg, s, p, ReleaseBoxOptions{
			BoxIndexName: opts.BoxIndexName,
		}); err != nil {
			return fmt.Errorf(
				"Cannot exclude '%s': this machine is its write owner, and giving that up "+
					"requires pushing the boxmeta, which failed (%w).\n"+
					"Excluding anyway would leave the box owned by a machine that no longer has "+
					"it, which no machine could then push. Run `boxyard release -r '%s'` once the "+
					"remote is reachable, then exclude.",
				opts.BoxIndexName, err, opts.BoxIndexName)
		}
		fmt.Fprintf(out, "Released write ownership of '%s' — excluding a box this machine "+
			"owned would otherwise leave it unpushable everywhere.\n", opts.BoxIndexName)
	}

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return err
	}
	defer release()

	if !opts.SkipSync {
		if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
			BoxIndexName: opts.BoxIndexName,
			Setting:      enums.SyncCareful,
			SkipLock:     true,
		}); err != nil {
			return err
		}
	}

	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		return err
	}
	if err := removeTreeWithRetry(dataPath); err != nil {
		return err
	}
	// The DATA sync record has to go too, or the next status probe sees a
	// record with no data and calls it an error.
	if err := os.Remove(bm.LocalSyncRecordPath(cfg, enums.PartData)); err != nil && !os.IsNotExist(err) {
		return err
	}

	fmt.Fprintf(out, "Excluded box '%s'\n", bm.Name)
	return nil
}

// removeTreeWithRetry deletes a directory, retrying on ENOTEMPTY.
//
// macOS Finder recreates .DS_Store inside a directory WHILE it is being
// deleted, so a single pass loses a race it did not know it was in.
func removeTreeWithRetry(path string) error {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if err := os.RemoveAll(path); err == nil {
			return nil
		} else if !errors.Is(err, syscall.ENOTEMPTY) {
			return err
		} else {
			lastErr = err
		}
		time.Sleep(100 * time.Millisecond)
	}
	return lastErr
}
