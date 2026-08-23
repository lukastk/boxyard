// Package storage adapts the rclone client to the interfaces the domain
// packages declare.
//
// The domain packages (syncengine, tombstones, remoteindex) each take an
// interface expressed as (remote, path) string pairs, deliberately: it is what
// lets the sync ordering logic — the part that protects a box from being
// overwritten — be tested exhaustively without a remote, which is how the
// 2400-scenario differential against Python was possible at all.
//
// rclone.Client instead speaks Location{Remote, Path}, because that is the
// shape rclone's own argv takes. This package is the seam between the two. It
// holds no decisions of its own: every method is a translation, so there is
// nothing here that can disagree with Python.
package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/lukastk/boxyard/internal/tombstones"
)

// Adapter satisfies syncengine.Storage, tombstones.Store and
// remoteindex.Store over one rclone client.
type Adapter struct {
	Client *rclone.Client
}

// New returns an Adapter over c.
func New(c *rclone.Client) *Adapter { return &Adapter{Client: c} }

// Compile-time proof that the one adapter covers every consumer. If a domain
// package grows a method, this fails to build here rather than at a call site.
var (
	_ syncengine.Prober  = (*Adapter)(nil)
	_ syncengine.Storage = (*Adapter)(nil)
	_ tombstones.Store   = (*Adapter)(nil)
	_ remoteindex.Store  = indexAdapter{}
)

// loc builds a Location. An empty remote means the local filesystem, which is
// exactly what rclone.Location's zero Remote already means — so the two
// conventions line up and no special case is needed.
func loc(remote, path string) rclone.Location {
	return rclone.Location{Remote: remote, Path: path}
}

func (a *Adapter) PathExists(ctx context.Context, remote, path string) (bool, bool, error) {
	return a.Client.PathExists(ctx, loc(remote, path))
}

// ReadSyncRecord returns nil when there is no record. A missing record is a
// legitimate expected state — a box that has never been synced — and must not
// be reported as an error, or every first sync would fail.
func (a *Adapter) ReadSyncRecord(ctx context.Context, remote, path string) (*models.SyncRecord, error) {
	exists, content, err := a.Client.Cat(ctx, loc(remote, path))
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, nil
	}
	var rec models.SyncRecord
	if err := strict.UnmarshalJSON([]byte(content), &rec); err != nil {
		return nil, fmt.Errorf("sync record at %s: %w", loc(remote, path).Spec(), err)
	}
	return &rec, nil
}

// WriteSyncRecord serialises with the pydantic-compatible encoder, not the
// stdlib one. The two differ (HTML escaping, and the timestamp's fractional
// part), and a record written by Go must be byte-identical to one written by
// Python — the ULIDs in these files are what prove which machine owns an
// in-flight sync.
func (a *Adapter) WriteSyncRecord(ctx context.Context, remote, path string, rec models.SyncRecord) error {
	body, err := strict.MarshalJSONCompact(rec)
	if err != nil {
		return err
	}
	if _, err := a.Client.Write(ctx, loc(remote, path), string(body)); err != nil {
		return err
	}
	return nil
}

func (a *Adapter) Mkdir(ctx context.Context, remote, path string) error {
	return a.Client.Mkdir(ctx, loc(remote, path))
}

func (a *Adapter) Purge(ctx context.Context, remote, path string) error {
	_, err := a.Client.Purge(ctx, loc(remote, path))
	return err
}

// Sync maps the engine's flat options onto rclone's nested ones. BackupPath is
// passed through untouched: it is already an rclone destination spec
// ("remote:path"), not a (remote, path) pair, because that is how the Python
// builds it.
func (a *Adapter) Sync(ctx context.Context, opts syncengine.SyncOptions) (bool, string, string, error) {
	out, err := a.Client.Sync(ctx,
		loc(opts.Source, opts.SourcePath),
		loc(opts.Dest, opts.DestPath),
		rclone.SyncOptions{
			TransferOptions: rclone.TransferOptions{
				Include:     opts.Include,
				Exclude:     opts.Exclude,
				Filter:      opts.Filter,
				IncludeFile: opts.IncludeFile,
				ExcludeFile: opts.ExcludeFile,
				FiltersFile: opts.FiltersFile,
				Progress:    opts.ShowProgress,
			},
			BackupPath: opts.BackupPath,
		})
	return out.OK, out.Stdout, out.Stderr, err
}

func (a *Adapter) Write(ctx context.Context, remote, path, content string) error {
	_, err := a.Client.Write(ctx, loc(remote, path), content)
	return err
}

func (a *Adapter) Cat(ctx context.Context, remote, path string) (bool, string, error) {
	return a.Client.Cat(ctx, loc(remote, path))
}

func (a *Adapter) Delete(ctx context.Context, remote, path string) error {
	_, err := a.Client.Delete(ctx, loc(remote, path))
	return err
}

// LocalIsEmptyDir reports whether a local directory has no entries.
func (a *Adapter) LocalIsEmptyDir(path string) (bool, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return false, err
	}
	return len(entries) == 0, nil
}

// LocalLastModified is the Go half of Python's check_last_time_modified, and
// the two must agree: this answer decides whether a box looks modified, and a
// disagreement between implementations is a box that syncs differently
// depending on which binary ran.
//
// Two rules carried over deliberately:
//
//   - excludeNames are skipped, so debris that can never be transferred cannot
//     make a box look modified. Without this a `.DS_Store` alone flips a box to
//     NEEDS_PUSH, and to CONFLICT if the remote also moved.
//   - An unreadable directory is a LOUD error, never skipped. Swallowing it
//     lowers the reported mtime, so a box with real changes underneath looks
//     SYNCED and is never pushed -- data loss by omission, with no error
//     anywhere. A directory that VANISHES mid-walk is different: that race is
//     legitimate (a build dir being cleaned) and is tolerated.
func (a *Adapter) LocalLastModified(path string, excludeNames map[string]bool) (time.Time, bool, error) {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return time.Time{}, false, nil
		}
		return time.Time{}, false, err
	}
	if !info.IsDir() {
		if excludeNames[filepath.Base(path)] {
			return time.Time{}, false, nil
		}
		return info.ModTime(), true, nil
	}

	var newest time.Time
	found := false
	var walk func(dir string) error
	walk = func(dir string) error {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				return nil // vanished mid-walk -- a legitimate race
			}
			return err
		}
		for _, e := range entries {
			if excludeNames[e.Name()] {
				continue
			}
			full := filepath.Join(dir, e.Name())
			if e.IsDir() {
				if err := walk(full); err != nil {
					return err
				}
				continue
			}
			fi, err := e.Info()
			if err != nil {
				if os.IsNotExist(err) {
					continue // vanished between listing and stat
				}
				return err
			}
			if !found || fi.ModTime().After(newest) {
				newest = fi.ModTime()
				found = true
			}
		}
		return nil
	}
	if err := walk(path); err != nil {
		return time.Time{}, false, err
	}
	return newest, found, nil
}

// ListJSON returns a listing for tombstones. An ABSENT directory yields an
// empty slice and no error: for tombstones that genuinely means "nothing has
// been deleted here", and the callers rely on the distinction.
func (a *Adapter) ListJSON(ctx context.Context, remote, path string) ([]tombstones.Entry, error) {
	entries, found, err := a.Client.Lsjson(ctx, loc(remote, path), rclone.LsjsonOptions{})
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, nil
	}
	out := make([]tombstones.Entry, 0, len(entries))
	for _, e := range entries {
		out = append(out, tombstones.Entry{Name: e.Name, IsDir: e.IsDir})
	}
	return out, nil
}

// indexAdapter re-types ListJSON for remoteindex, whose Entry is a distinct
// (if identical) type. Go will not let one method satisfy both interfaces, and
// duplicating the type rather than sharing one is the domain packages' own
// choice — they do not depend on each other.
type indexAdapter struct{ *Adapter }

// ForRemoteIndex returns a view of this adapter usable as a remoteindex.Store.
func (a *Adapter) ForRemoteIndex() remoteindex.Store { return indexAdapter{a} }

func (i indexAdapter) ListJSON(ctx context.Context, remote, path string) ([]remoteindex.Entry, error) {
	entries, found, err := i.Client.Lsjson(ctx, loc(remote, path), rclone.LsjsonOptions{})
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, nil
	}
	out := make([]remoteindex.Entry, 0, len(entries))
	for _, e := range entries {
		out = append(out, remoteindex.Entry{Name: e.Name, IsDir: e.IsDir})
	}
	return out, nil
}
