// Package tombstones records boxes that have been deleted.
//
// A tombstone lives on the remote at
// {store_path}/tombstones/{box_id}.json and stops other machines from
// resurrecting a box that was deliberately removed: a machine that still has
// the box locally sees the tombstone and skips the sync rather than pushing it
// back.
//
// Ported from src/boxyard/_tombstones.py.
package tombstones

import (
	"context"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/lukastk/boxyard/internal/sysinfo"
)

// Store is the remote access tombstones need. Declared here, in the consumer,
// so this package does not depend on the rclone implementation.
type Store interface {
	Write(ctx context.Context, remote, path, content string) error
	Cat(ctx context.Context, remote, path string) (exists bool, content string, err error)
	PathExists(ctx context.Context, remote, path string) (exists, isDir bool, err error)
	ListJSON(ctx context.Context, remote, path string) ([]Entry, error)
	Delete(ctx context.Context, remote, path string) error
}

// Entry is one item from a remote listing.
type Entry struct {
	Name  string
	IsDir bool
}

// Tombstone marks a deleted box.
type Tombstone struct {
	BoxID             string `json:"box_id"`
	DeletedAtUTC      string `json:"deleted_at_utc"`
	DeletedByHostname string `json:"deleted_by_hostname"`
	LastKnownName     string `json:"last_known_name"`
}

// Validate mirrors the Python StrictModel's required fields.
func (t *Tombstone) Validate() error {
	const ty = "Tombstone"
	if err := strict.RequireNonZero(ty, "box_id", t.BoxID); err != nil {
		return err
	}
	if err := strict.RequireNonZero(ty, "deleted_at_utc", t.DeletedAtUTC); err != nil {
		return err
	}
	if err := strict.RequireNonZero(ty, "deleted_by_hostname", t.DeletedByHostname); err != nil {
		return err
	}
	if err := strict.RequireNonZero(ty, "last_known_name", t.LastKnownName); err != nil {
		return err
	}
	if _, err := t.DeletedAt(); err != nil {
		return strict.Invalid(ty, "deleted_at_utc", err.Error())
	}
	return nil
}

// DeletedAt parses the deletion time. Pydantic writes the fractional part only
// when it is non-zero, so both forms occur on the remote.
func (t *Tombstone) DeletedAt() (time.Time, error) {
	for _, layout := range []string{
		"2006-01-02T15:04:05.000000Z",
		"2006-01-02T15:04:05Z",
		time.RFC3339Nano,
		time.RFC3339,
	} {
		if ts, err := time.Parse(layout, t.DeletedAtUTC); err == nil {
			return ts.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("unparseable deletion time %q", t.DeletedAtUTC)
}

// RelPath is a tombstone's path relative to a storage location's store root.
func RelPath(boxID string) string {
	return "tombstones/" + boxID + ".json"
}

func fullPath(cfg *config.Config, storageLocation, boxID string) (string, error) {
	sl, err := cfg.StorageLocation(storageLocation)
	if err != nil {
		return "", err
	}
	return path.Join(sl.StorePath, RelPath(boxID)), nil
}

func dirPath(cfg *config.Config, storageLocation string) (string, error) {
	sl, err := cfg.StorageLocation(storageLocation)
	if err != nil {
		return "", err
	}
	return path.Join(sl.StorePath, "tombstones"), nil
}

// Create writes a tombstone for a deleted box.
func Create(ctx context.Context, s Store, cfg *config.Config, storageLocation, boxID, lastKnownName string) (*Tombstone, error) {
	t := &Tombstone{
		BoxID:             boxID,
		DeletedAtUTC:      strict.FormatPydanticTime(time.Now().UTC()),
		DeletedByHostname: sysinfo.Hostname(),
		LastKnownName:     lastKnownName,
	}
	if err := t.Validate(); err != nil {
		return nil, err
	}
	body, err := strict.MarshalJSONCompact(t)
	if err != nil {
		return nil, err
	}
	p, err := fullPath(cfg, storageLocation, boxID)
	if err != nil {
		return nil, err
	}
	if err := s.Write(ctx, storageLocation, p, string(body)); err != nil {
		return nil, err
	}
	return t, nil
}

// IsTombstoned reports whether a box has been deleted.
func IsTombstoned(ctx context.Context, s Store, cfg *config.Config, storageLocation, boxID string) (bool, error) {
	p, err := fullPath(cfg, storageLocation, boxID)
	if err != nil {
		return false, err
	}
	exists, _, err := s.PathExists(ctx, storageLocation, p)
	return exists, err
}

// Get returns a box's tombstone, or nil if there is none.
//
// A missing tombstone is a legitimate expected state — most boxes have none —
// so it is nil, not an error. A tombstone that exists but does not parse IS an
// error: it means the remote holds corrupt state.
func Get(ctx context.Context, s Store, cfg *config.Config, storageLocation, boxID string) (*Tombstone, error) {
	p, err := fullPath(cfg, storageLocation, boxID)
	if err != nil {
		return nil, err
	}
	exists, content, err := s.Cat(ctx, storageLocation, p)
	if err != nil {
		return nil, err
	}
	if !exists || content == "" {
		return nil, nil
	}
	var t Tombstone
	if err := strict.UnmarshalJSON([]byte(content), &t); err != nil {
		return nil, fmt.Errorf("tombstone %s is corrupt: %w", p, err)
	}
	return &t, nil
}

// List returns every tombstone at a storage location.
func List(ctx context.Context, s Store, cfg *config.Config, storageLocation string) ([]*Tombstone, error) {
	dir, err := dirPath(cfg, storageLocation)
	if err != nil {
		return nil, err
	}
	entries, err := s.ListJSON(ctx, storageLocation, dir)
	if err != nil {
		return nil, err
	}
	// No tombstones directory at all is normal on a fresh remote.
	if entries == nil {
		return []*Tombstone{}, nil
	}

	out := []*Tombstone{}
	for _, e := range entries {
		if e.IsDir || !strings.HasSuffix(e.Name, ".json") {
			continue
		}
		exists, content, err := s.Cat(ctx, storageLocation, path.Join(dir, e.Name))
		if err != nil {
			return nil, err
		}
		if !exists || content == "" {
			continue
		}
		var t Tombstone
		if err := strict.UnmarshalJSON([]byte(content), &t); err != nil {
			return nil, fmt.Errorf("tombstone %s is corrupt: %w", e.Name, err)
		}
		out = append(out, &t)
	}
	return out, nil
}

// ListBoxIDs returns every tombstoned box id at a storage location, from a
// SINGLE listing.
//
// This exists because the per-box probe did not scale. A `multi-sync` pass over
// 587 boxes made 587 separate SFTP connections to the same storage box, every
// 20 minutes, on every machine — which saturated the server's connection limit
// and was measurably failing ~8 boxes per pass on three machines with
// "couldn't initialise SFTP" (Python v0.5.1). Callers hold this set for the
// whole pass and check membership.
//
// Unlike List, it never reads a tombstone's CONTENT: the filename is the box
// id, and membership is all a sync decision needs.
//
// A listing failure is an ERROR, and that is the whole safety property: an
// empty set means "nothing is tombstoned", and returning that when we simply
// could not look would let a box another machine deleted be silently
// resurrected here. A MISSING tombstones directory is different — it genuinely
// means nothing has ever been deleted at this location — and is the only case
// that yields an empty set.
func ListBoxIDs(ctx context.Context, s Store, cfg *config.Config, storageLocation string) (map[string]bool, error) {
	dir, err := dirPath(cfg, storageLocation)
	if err != nil {
		return nil, err
	}
	entries, err := s.ListJSON(ctx, storageLocation, dir)
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	// A nil slice means the directory does not exist — see above.
	for _, e := range entries {
		if e.IsDir || !strings.HasSuffix(e.Name, ".json") {
			continue
		}
		out[strings.TrimSuffix(e.Name, ".json")] = true
	}
	return out, nil
}

// Remove deletes a tombstone, resurrecting the box id. Reports whether there
// was one to remove.
func Remove(ctx context.Context, s Store, cfg *config.Config, storageLocation, boxID string) (bool, error) {
	p, err := fullPath(cfg, storageLocation, boxID)
	if err != nil {
		return false, err
	}
	exists, _, err := s.PathExists(ctx, storageLocation, p)
	if err != nil {
		return false, err
	}
	if !exists {
		return false, nil
	}
	if err := s.Delete(ctx, storageLocation, p); err != nil {
		return false, err
	}
	return true, nil
}
