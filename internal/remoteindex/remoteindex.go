// Package remoteindex maps a box id to the index name it has on a remote.
//
// A box can be renamed on one machine and not another, so a box id does not
// determine its remote directory name. Resolving one requires listing the
// remote, which is slow over SFTP, so the answer is cached locally at
// {boxyard_data_path}/remote_indexes/{storage_location}.json.
//
// The cache is purely a local optimisation: it is never synced, and every
// lookup verifies its answer against the remote before returning it.
//
// Ported from src/boxyard/_remote_index.py.
package remoteindex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
)

// Entry is one item from a remote listing.
type Entry struct {
	Name  string
	IsDir bool
}

// Store is the remote access this package needs.
type Store interface {
	PathExists(ctx context.Context, remote, path string) (exists, isDir bool, err error)
	ListJSON(ctx context.Context, remote, path string) ([]Entry, error)
}

// CachePath is where a storage location's index cache lives.
func CachePath(cfg *config.Config, storageLocation string) string {
	return filepath.Join(cfg.RemoteIndexesPath(), storageLocation+".json")
}

// Load reads the cache, mapping box id to remote index name.
//
// An ABSENT cache is the normal cold-start state, and a CORRUPT one is treated
// as empty on purpose: this is a cache, every entry is re-verified against the
// remote before use, and a full rescan rebuilds it. Any other I/O failure —
// a permissions problem, say — IS returned, because that is a real fault that
// would otherwise cause a silent full rescan on every single command.
func Load(cfg *config.Config, storageLocation string) (map[string]string, error) {
	p := CachePath(cfg, storageLocation)
	raw, err := os.ReadFile(p)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return map[string]string{}, nil
		}
		return nil, fmt.Errorf("cannot read the remote index cache %s: %w", p, err)
	}
	var cache map[string]string
	if err := json.Unmarshal(raw, &cache); err != nil {
		// Self-healing: discard and rebuild.
		return map[string]string{}, nil
	}
	if cache == nil {
		cache = map[string]string{}
	}
	return cache, nil
}

// Save writes the cache.
func Save(cfg *config.Config, storageLocation string, cache map[string]string) error {
	p := CachePath(cfg, storageLocation)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	// Two-space indent, matching the Python. Key ORDER differs — Go sorts map
	// keys, Python preserves insertion order — which is harmless: the file is
	// local, never synced, and read only as a map.
	body, err := json.MarshalIndent(cache, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(p, body, 0o644)
}

// Update sets one entry.
func Update(cfg *config.Config, storageLocation, boxID, indexName string) error {
	cache, err := Load(cfg, storageLocation)
	if err != nil {
		return err
	}
	cache[boxID] = indexName
	return Save(cfg, storageLocation, cache)
}

// Remove drops one entry.
func Remove(cfg *config.Config, storageLocation, boxID string) error {
	cache, err := Load(cfg, storageLocation)
	if err != nil {
		return err
	}
	if _, ok := cache[boxID]; !ok {
		return nil
	}
	delete(cache, boxID)
	return Save(cfg, storageLocation, cache)
}

func boxesPath(cfg *config.Config, storageLocation string) (string, error) {
	sl, err := cfg.StorageLocation(storageLocation)
	if err != nil {
		return "", err
	}
	return path.Join(sl.StorePath, boxconst.RemoteBoxesRelPath), nil
}

// Find resolves a box id to its remote index name, or "" if the box is not on
// the remote.
//
// The cached answer is always VERIFIED against the remote before being
// returned — a box renamed from another machine would otherwise resolve to a
// directory that no longer exists.
func Find(ctx context.Context, s Store, cfg *config.Config, storageLocation, boxID string) (string, error) {
	boxes, err := boxesPath(cfg, storageLocation)
	if err != nil {
		return "", err
	}
	cache, err := Load(cfg, storageLocation)
	if err != nil {
		return "", err
	}

	if cached, ok := cache[boxID]; ok {
		exists, _, err := s.PathExists(ctx, storageLocation, path.Join(boxes, cached))
		if err != nil {
			return "", err
		}
		if exists {
			return cached, nil
		}
		// Stale: the box was renamed or removed elsewhere.
		delete(cache, boxID)
		if err := Save(cfg, storageLocation, cache); err != nil {
			return "", err
		}
	}

	entries, err := s.ListJSON(ctx, storageLocation, boxes)
	if err != nil {
		return "", err
	}
	for _, e := range entries {
		if e.IsDir && strings.HasPrefix(e.Name, boxID+"__") {
			cache[boxID] = e.Name
			if err := Save(cfg, storageLocation, cache); err != nil {
				return "", err
			}
			return e.Name, nil
		}
	}

	if _, ok := cache[boxID]; ok {
		delete(cache, boxID)
		if err := Save(cfg, storageLocation, cache); err != nil {
			return "", err
		}
	}
	return "", nil
}

// Rebuild rescans the remote and replaces the whole cache.
func Rebuild(ctx context.Context, s Store, cfg *config.Config, storageLocation string) (map[string]string, error) {
	boxes, err := boxesPath(cfg, storageLocation)
	if err != nil {
		return nil, err
	}
	entries, err := s.ListJSON(ctx, storageLocation, boxes)
	if err != nil {
		return nil, err
	}
	cache := map[string]string{}
	for _, e := range entries {
		if !e.IsDir {
			continue
		}
		boxID, err := models.ExtractBoxID(e.Name)
		if err != nil {
			// A directory whose name is not an index name is not a box.
			// `boxyard doctor` reports these; skipping here matches Python.
			continue
		}
		cache[boxID] = e.Name
	}
	if err := Save(cfg, storageLocation, cache); err != nil {
		return nil, err
	}
	return cache, nil
}
