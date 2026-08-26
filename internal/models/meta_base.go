package models

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
)

// The META merge base: the boxmeta as it stood at the last successful META
// sync, kept beside the LOCAL sync record and never synced anywhere.
//
// It exists because a boxmeta both sides have edited is currently a dead end —
// sync sees two records that disagree, cannot tell which fields moved on which
// side, and refuses. That is how 44 boxes on macbook stopped propagating their
// groups for a day in August 2026 while every other machine reported "all
// checks passed".
//
// Keeping it LOCAL is what makes this cheap. The alternative proposed for the
// same problem — giving `write_owner` its own synced file so ownership churn
// stops bumping META's sync record — costs one more remote round trip per box
// per pass, forever, and this repo has already had a per-box remote call
// saturate the storage box's connection limit.
//
// The rule everything here protects: a STALE base is worse than no base. A
// merge computes what each side changed by diffing against it, so a base that
// never corresponded to a real shared state produces a confidently wrong
// answer, where a missing one only makes the merge decline.

// LocalMetaBasePath is where the base for this box lives.
func (b *BoxMeta) LocalMetaBasePath(cfg *config.Config) string {
	return filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, b.IndexName(), "meta.base.toml")
}

// RecordMetaBase snapshots the boxmeta that local and remote have just agreed
// on.
//
// Call this ONLY where the two sides are known to match: the META sync
// reported SYNCED, or a transfer completed and was verified. Anywhere else
// records a base that never was.
func RecordMetaBase(cfg *config.Config, bm *BoxMeta) error {
	source, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		return err
	}
	dest := bm.LocalMetaBasePath(cfg)

	if fi, err := os.Stat(source); err != nil || fi.IsDir() {
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("cannot read '%s': %w", source, err)
		}
		// No local boxmeta means there is no agreed state to record. Drop any
		// base rather than keep one that no longer describes anything.
		return removeIfPresent(dest)
	}

	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return fmt.Errorf("cannot create '%s': %w", filepath.Dir(dest), err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(dest), ".meta.base.*.tmp")
	if err != nil {
		return fmt.Errorf("cannot create a temporary file beside '%s': %w", dest, err)
	}
	tmpName := tmp.Name()

	// Written through a temp file and renamed, so a base is never half-written;
	// and on ANY failure the previous base is removed rather than left
	// describing a state that has passed.
	if err := func() error {
		defer tmp.Close()
		in, err := os.Open(source)
		if err != nil {
			return err
		}
		defer in.Close()
		if _, err := io.Copy(tmp, in); err != nil {
			return err
		}
		return tmp.Sync()
	}(); err != nil {
		os.Remove(tmpName)
		_ = removeIfPresent(dest)
		return fmt.Errorf("cannot record the META merge base for '%s': %w", bm.IndexName(), err)
	}

	if err := os.Rename(tmpName, dest); err != nil {
		os.Remove(tmpName)
		_ = removeIfPresent(dest)
		return fmt.Errorf("cannot record the META merge base for '%s': %w", bm.IndexName(), err)
	}
	return nil
}

// ReadMetaBase returns the recorded base, or nil when there is not one to
// trust.
//
// nil is a legitimate, expected state — a box that has not synced its META
// since the base was introduced simply has none — so it is returned rather
// than reported as an error. A base that exists but does not PARSE is
// different: that is corruption, and merging against a half-read file would be
// worse than declining, so it is removed and nil returned.
func ReadMetaBase(cfg *config.Config, bm *BoxMeta) (*BoxMeta, error) {
	path := bm.LocalMetaBasePath(cfg)
	fi, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		// An error other than "not there" on a file in the boxyard data
		// directory is a real problem; quietly deleting the base on the way
		// past would hide it.
		return nil, fmt.Errorf("cannot read the META merge base for '%s': %w", bm.IndexName(), err)
	}
	if fi.IsDir() {
		return nil, fmt.Errorf("the META merge base for '%s' is a directory", bm.IndexName())
	}

	base, err := LoadBoxMetaFromPath(path, BoxIdentity{
		CreationTimestampUTC: bm.CreationTimestampUTC,
		BoxSubid:             bm.BoxSubid,
		Name:                 bm.Name,
		StorageLocation:      bm.StorageLocation,
	})
	if err != nil {
		// Unparseable, or valid TOML that is not a boxmeta. Either way there
		// is nothing here to diff against, and leaving it would make every
		// later read pay the same cost.
		if rmErr := removeIfPresent(path); rmErr != nil {
			return nil, rmErr
		}
		return nil, nil
	}
	return base, nil
}

func removeIfPresent(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("cannot remove '%s': %w", path, err)
	}
	return nil
}
