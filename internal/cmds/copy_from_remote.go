package cmds

import (
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// Copier is the plain copy CopyFromRemote needs — a transfer OUT of the yard,
// with no sync records and no state changes.
type Copier interface {
	Copy(ctx context.Context, src, dst rclone.Location, o rclone.TransferOptions) (rclone.Output, error)
	Copyto(ctx context.Context, src, dst rclone.Location, o rclone.CopytoOptions) (rclone.Output, error)
}

// CopyStore is what CopyFromRemote needs from storage.
type CopyStore interface {
	SyncStore
	Copier
}

// CopyFromRemoteOptions mirrors the Python `copy_from_remote` signature.
type CopyFromRemoteOptions struct {
	BoxIndexName       string
	DestPath           string
	CopyMeta           bool
	CopyConf           bool
	Overwrite          bool
	ShowRcloneProgress bool
	Verbose            bool
	Out                io.Writer
}

// CopyFromRemote copies a box's remote contents to an ordinary directory,
// outside the yard. It returns the destination.
//
// Ported from pts/mod/cmds/12_copy_from_remote.pct.py.
func CopyFromRemote(ctx context.Context, cfg *config.Config, s CopyStore, p syncengine.Perms, opts CopyFromRemoteOptions) (string, error) {
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
		return "", fmt.Errorf("Box '%s' does not exist locally.", opts.BoxIndexName)
	}

	destPath, err := filepath.Abs(opts.DestPath)
	if err != nil {
		return "", err
	}
	// The destination must be OUTSIDE the managed trees. Copying into them
	// would create a directory that looks like a box registration but is not
	// one, which breaks the next registry refresh for the whole yard.
	for _, guard := range []struct{ name, root string }{
		{"boxyard data path", cfg.BoxyardDataPath},
		{"user boxes path", cfg.UserBoxesPath},
	} {
		root, err := filepath.Abs(guard.root)
		if err != nil {
			return "", err
		}
		if isWithin(destPath, root) {
			return "", fmt.Errorf(
				"Destination path '%s' is within the %s '%s'. This operation is not allowed to "+
					"prevent conflicts with managed storage. Use a path outside of '%s'.",
				destPath, guard.name, root, root)
		}
	}
	if _, err := os.Stat(destPath); err == nil && !opts.Overwrite {
		return "", fmt.Errorf(
			"Destination path '%s' already exists. Use --overwrite to overwrite existing files.",
			destPath)
	}

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, bm.StorageLocation, bm.BoxID())
	if err != nil {
		return "", err
	}
	if remoteIndexName == "" {
		return "", fmt.Errorf(
			"Box '%s' not found on remote storage '%s'. The box may have been deleted or the "+
				"remote is not accessible.",
			opts.BoxIndexName, bm.StorageLocation)
	}
	printf("Found remote box: %s\n", remoteIndexName)

	remoteBox := path.Join(slConfig.StorePath, boxconst.RemoteBoxesRelPath, remoteIndexName)

	printf("Copying DATA from %s:%s to %s\n", bm.StorageLocation,
		path.Join(remoteBox, boxconst.BoxDataRelPath), destPath)
	if err := os.MkdirAll(destPath, 0o755); err != nil {
		return "", err
	}
	out, err := s.Copy(ctx,
		rclone.Remote(bm.StorageLocation, path.Join(remoteBox, boxconst.BoxDataRelPath)),
		rclone.Local(destPath),
		rclone.TransferOptions{Progress: opts.ShowRcloneProgress})
	if err != nil {
		return "", err
	}
	if !out.OK {
		return "", fmt.Errorf("Failed to copy DATA from remote: %s", out.Stderr)
	}
	// Restore the exec bits the transport dropped. Additive; a no-op when there
	// is no manifest.
	if _, err := p.Apply(destPath); err != nil {
		return "", err
	}
	printf("DATA copied successfully.\n")

	for _, part := range []struct {
		want       bool
		label, rel string
		isFile     bool
	}{
		{opts.CopyMeta, "META", boxconst.BoxMetafileRelPath, true},
		{opts.CopyConf, "CONF", boxconst.BoxConfRelPath, false},
	} {
		if !part.want {
			continue
		}
		src := rclone.Remote(bm.StorageLocation, path.Join(remoteBox, part.rel))
		dst := filepath.Join(destPath, part.rel)
		printf("Copying %s from %s:%s\n", part.label, bm.StorageLocation, path.Join(remoteBox, part.rel))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return "", err
		}
		var out rclone.Output
		if part.isFile {
			// copyto, not copy: the destination is one exact file name.
			out, err = s.Copyto(ctx, src, rclone.Local(dst), rclone.CopytoOptions{Progress: opts.ShowRcloneProgress})
		} else {
			out, err = s.Copy(ctx, src, rclone.Local(dst), rclone.TransferOptions{Progress: opts.ShowRcloneProgress})
		}
		if err != nil {
			return "", err
		}
		if !out.OK {
			return "", fmt.Errorf("Failed to copy %s from remote: %s", part.label, out.Stderr)
		}
	}
	return destPath, nil
}

// isWithin reports whether p is inside root (or is root itself).
func isWithin(p, root string) bool {
	rel, err := filepath.Rel(root, p)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
