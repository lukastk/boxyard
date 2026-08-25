package cmds

import (
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/naming"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/remoteindex"
)

// Mover is the remote rename RenameBox needs.
type Mover interface {
	Moveto(ctx context.Context, src, dst rclone.Location) (rclone.Output, error)
}

// RenameStore is everything RenameBox needs from storage.
type RenameStore interface {
	SyncStore
	Mover
}

// RenameBoxOptions mirrors the Python `rename_box` signature.
type RenameBoxOptions struct {
	BoxIndexName string
	NewName      string
	// Scope defaults to BOTH.
	Scope   enums.RenameScope
	Verbose bool
	Out     io.Writer
}

// RenameBox renames a box, keeping its box id.
//
// Ported from pts/mod/cmds/10_rename_box.pct.py.
func RenameBox(ctx context.Context, cfg *config.Config, s RenameStore, opts RenameBoxOptions) (string, error) {
	scope := opts.Scope
	if scope == "" {
		scope = enums.RenameBoth
	}
	if !scope.Valid() {
		return "", fmt.Errorf("invalid rename scope: %q", scope)
	}
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return "", fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}
	boxID, err := models.ExtractBoxID(opts.BoxIndexName)
	if err != nil {
		return "", err
	}
	storageLocation := bm.StorageLocation

	// A REMOTE-scoped rename renames the box's directory on the remote, so it
	// is a write to shared state and needs the ownership gate. A LOCAL-scope
	// rename touches only this machine and is deliberately left alone: a
	// read-only replica may still call its own copy whatever it likes.
	if scope == enums.RenameRemote || scope == enums.RenameBoth {
		onDisk, err := models.LoadBoxMeta(cfg, storageLocation, opts.BoxIndexName)
		if err != nil {
			return "", err
		}
		if err := ownership.OwnerGate(cfg, onDisk,
			fmt.Sprintf("rename '%s' on the remote", opts.BoxIndexName)); err != nil {
			return "", err
		}
	}

	// The name is used verbatim as a directory name, so it has to be a single
	// path component.
	if err := naming.ValidateBoxName(opts.NewName); err != nil {
		return "", err
	}
	newIndexName := boxID + "__" + opts.NewName
	printf("Renaming box from '%s' to '%s'\n", bm.Name, opts.NewName)
	printf("Index name: %s -> %s\n", opts.BoxIndexName, newIndexName)

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return "", err
	}
	defer release()

	if scope == enums.RenameLocal || scope == enums.RenameBoth {
		printf("Renaming locally...\n")
		renamed := *bm
		renamed.Name = opts.NewName

		moves := [][2]string{
			{filepath.Join(cfg.LocalStorePath(), storageLocation, opts.BoxIndexName),
				filepath.Join(cfg.LocalStorePath(), storageLocation, newIndexName)},
			{filepath.Join(cfg.UserBoxesPath, opts.BoxIndexName),
				filepath.Join(cfg.UserBoxesPath, newIndexName)},
			{filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, opts.BoxIndexName),
				filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, newIndexName)},
		}
		for _, m := range moves {
			if _, err := os.Lstat(m[0]); err != nil {
				if os.IsNotExist(err) {
					continue
				}
				return "", err
			}
			if err := os.Rename(m[0], m[1]); err != nil {
				return "", err
			}
		}
		// Saved AFTER the directories move: Save derives its path from the
		// index name, so writing first would put the file where the box no
		// longer is.
		if err := renamed.Save(cfg); err != nil {
			return "", err
		}
		printf("Local rename complete.\n")
	}

	if scope == enums.RenameRemote || scope == enums.RenameBoth {
		slConfig, err := bm.StorageLocationConfig(cfg)
		if err != nil {
			return "", err
		}
		if slConfig.StorageType == config.StorageLocal {
			printf("Skipping remote rename for local storage location.\n")
		} else if err := renameOnRemote(ctx, cfg, s, storageLocation, slConfig.StorePath,
			boxID, newIndexName, printf); err != nil {
			return "", err
		}
	}

	if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
		return "", err
	}
	return newIndexName, nil
}

func renameOnRemote(ctx context.Context, cfg *config.Config, s RenameStore,
	storageLocation, storePath, boxID, newIndexName string, printf func(string, ...any)) error {
	printf("Renaming on remote...\n")

	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, storageLocation, boxID)
	if err != nil {
		return err
	}
	if remoteIndexName == "" {
		// Absence here really means absence: the listing helpers report "not
		// found" only for rclone's not-found exit codes and error on any real
		// failure. So this is the ordinary case of a box created and renamed
		// before its first sync.
		printf("This box is not on the remote yet, so there is nothing to rename there; " +
			"it will be pushed under its new name on the next sync.\n")
		return nil
	}

	boxesPath := path.Join(storePath, boxconst.RemoteBoxesRelPath)
	out, err := s.Moveto(ctx,
		rclone.Remote(storageLocation, path.Join(boxesPath, remoteIndexName)),
		rclone.Remote(storageLocation, path.Join(boxesPath, newIndexName)))
	if err != nil {
		return err
	}
	if !out.OK {
		return fmt.Errorf("Failed to rename remote box: %s", out.Stderr)
	}

	// The sync records move too, but a failure here is NOT fatal: the records
	// are a cache of what was last transferred, and the next sync rebuilds
	// them under the new name. Losing the box's directory rename halfway would
	// be far worse.
	recordsPath := path.Join(storePath, boxconst.SyncRecordsRelPath)
	recordOut, err := s.Moveto(ctx,
		rclone.Remote(storageLocation, path.Join(recordsPath, remoteIndexName)),
		rclone.Remote(storageLocation, path.Join(recordsPath, newIndexName)))
	if err != nil || !recordOut.OK {
		detail := ""
		if err != nil {
			detail = err.Error()
		} else {
			detail = recordOut.Stderr
		}
		printf("Warning: Failed to rename remote sync records: %s\n", detail)
	}

	if err := remoteindex.Update(cfg, storageLocation, boxID, newIndexName); err != nil {
		return err
	}
	printf("Remote rename complete.\n")
	return nil
}
