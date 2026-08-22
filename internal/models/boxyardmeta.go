package models

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/strict"
)

// BoxyardMeta is the yard-wide registry: every box registered on this machine.
//
// It is a CACHE, rebuilt by scanning the local store, and persisted to
// boxyard_meta.json. That file is a public contract — besides both boxyard
// implementations, it is read directly by mysystem's TypeScript BoxyardService
// and by myrig's box picker.
type BoxyardMeta struct {
	BoxMetas []*BoxMeta `json:"box_metas"`
}

// BrokenRegistration is a local-store directory that could not be loaded.
type BrokenRegistration struct {
	Registration string
	Err          error
}

// CreateBoxyardMeta rebuilds the registry by scanning the local store.
//
// A registration that cannot be loaded is collected and skipped rather than
// failing the whole refresh: one broken box must not make the rest of the yard
// unusable. `boxyard doctor` reports them in full.
func CreateBoxyardMeta(cfg *config.Config) (*BoxyardMeta, []BrokenRegistration, error) {
	meta := &BoxyardMeta{BoxMetas: []*BoxMeta{}}
	var broken []BrokenRegistration

	for storageLocationName := range cfg.StorageLocations {
		dir := filepath.Join(cfg.LocalStorePath(), storageLocationName)
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				// A storage location with nothing checked out locally is a
				// legitimate state, not an error.
				continue
			}
			return nil, nil, fmt.Errorf("cannot scan local store %s: %w", dir, err)
		}
		for _, e := range entries {
			// Python uses Path.glob("*"), which — verified empirically —
			// DOES include dotfiles, so hidden directories are scanned too.
			if !e.IsDir() {
				continue
			}
			bm, err := LoadBoxMeta(cfg, storageLocationName, e.Name())
			if err != nil {
				broken = append(broken, BrokenRegistration{
					Registration: storageLocationName + "/" + e.Name(),
					Err:          err,
				})
				continue
			}
			meta.BoxMetas = append(meta.BoxMetas, bm)
		}
	}
	return meta, broken, nil
}

// ReportBroken writes the same warning the Python prints to stderr.
func ReportBroken(w *os.File, broken []BrokenRegistration) {
	if len(broken) == 0 {
		return
	}
	fmt.Fprintf(w, "Warning: skipped %d unreadable box registration(s) — run `boxyard doctor` for details:\n", len(broken))
	for _, b := range broken {
		fmt.Fprintf(w, "  - %s: %v\n", b.Registration, b.Err)
	}
}

// Marshal renders the registry as boxyard_meta.json.
func (m *BoxyardMeta) Marshal() ([]byte, error) {
	for _, bm := range m.BoxMetas {
		bm.normalizeSlices()
	}
	if m.BoxMetas == nil {
		m.BoxMetas = []*BoxMeta{}
	}
	return strict.MarshalJSONCompact(m)
}

// RefreshBoxyardMeta rebuilds the registry and writes it out atomically, under
// the global lock.
func RefreshBoxyardMeta(cfg *config.Config, skipLock bool) (*BoxyardMeta, error) {
	if !skipLock {
		mgr := locking.NewManager(cfg.BoxyardDataPath)
		release, err := mgr.GlobalLock(locking.GlobalLockTimeout)
		if err != nil {
			return nil, err
		}
		defer release()
	}

	meta, broken, err := CreateBoxyardMeta(cfg)
	if err != nil {
		return nil, err
	}
	ReportBroken(os.Stderr, broken)

	data, err := meta.Marshal()
	if err != nil {
		return nil, err
	}
	metaPath := cfg.BoxyardMetaPath()
	if err := os.MkdirAll(filepath.Dir(metaPath), 0o755); err != nil {
		return nil, err
	}
	// Atomic write: temp file + rename, matching the Python's
	// with_suffix(".tmp"), which turns boxyard_meta.json into boxyard_meta.tmp.
	tmpPath := strings.TrimSuffix(metaPath, filepath.Ext(metaPath)) + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0o644); err != nil {
		return nil, err
	}
	if err := os.Rename(tmpPath, metaPath); err != nil {
		return nil, err
	}
	return meta, nil
}

// GetBoxyardMeta returns the registry, rebuilding it if the cache is absent or
// forceCreate is set.
func GetBoxyardMeta(cfg *config.Config, forceCreate bool) (*BoxyardMeta, error) {
	metaPath := cfg.BoxyardMetaPath()
	if _, err := os.Stat(metaPath); err != nil || forceCreate {
		if _, err := RefreshBoxyardMeta(cfg, false); err != nil {
			return nil, err
		}
	}
	var meta BoxyardMeta
	if err := strict.ReadJSONFile(metaPath, &meta); err != nil {
		return nil, err
	}
	for _, bm := range meta.BoxMetas {
		bm.normalizeSlices()
	}
	return &meta, nil
}

// unmarshalMeta is the strict decode used by GetBoxyardMeta, exposed for tests.
func unmarshalMeta(data []byte, m *BoxyardMeta) error {
	if err := strict.UnmarshalJSON(data, m); err != nil {
		return err
	}
	for _, bm := range m.BoxMetas {
		bm.normalizeSlices()
	}
	return nil
}

// --- lookups ---

func (m *BoxyardMeta) ByIndexName() map[string]*BoxMeta {
	out := make(map[string]*BoxMeta, len(m.BoxMetas))
	for _, bm := range m.BoxMetas {
		out[bm.IndexName()] = bm
	}
	return out
}

func (m *BoxyardMeta) ByBoxID() map[string]*BoxMeta {
	out := make(map[string]*BoxMeta, len(m.BoxMetas))
	for _, bm := range m.BoxMetas {
		out[bm.BoxID()] = bm
	}
	return out
}

func (m *BoxyardMeta) ByStorageLocation() map[string]map[string]*BoxMeta {
	out := map[string]map[string]*BoxMeta{}
	for _, bm := range m.BoxMetas {
		if out[bm.StorageLocation] == nil {
			out[bm.StorageLocation] = map[string]*BoxMeta{}
		}
		out[bm.StorageLocation][bm.IndexName()] = bm
	}
	return out
}

// Lookup returns the box with this index name, or a descriptive error.
func (m *BoxyardMeta) Lookup(indexName string) (*BoxMeta, error) {
	if bm, ok := m.ByIndexName()[indexName]; ok {
		return bm, nil
	}
	return nil, fmt.Errorf("Box '%s' not found.", indexName)
}

// --- parent/child DAG ---

func (m *BoxyardMeta) ChildrenOf(boxID string) []*BoxMeta {
	var out []*BoxMeta
	for _, bm := range m.BoxMetas {
		for _, p := range bm.Parents {
			if p == boxID {
				out = append(out, bm)
				break
			}
		}
	}
	return out
}

// DescendantsOf walks the DAG breadth-first, visiting each box once.
func (m *BoxyardMeta) DescendantsOf(boxID string) []*BoxMeta {
	visited := map[string]bool{}
	queue := []string{boxID}
	var result []*BoxMeta
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		for _, child := range m.ChildrenOf(current) {
			if visited[child.BoxID()] {
				continue
			}
			visited[child.BoxID()] = true
			result = append(result, child)
			queue = append(queue, child.BoxID())
		}
	}
	return result
}

func (m *BoxyardMeta) AncestorsOf(boxID string) []*BoxMeta {
	byID := m.ByBoxID()
	visited := map[string]bool{}
	queue := []string{boxID}
	var result []*BoxMeta
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		cur, ok := byID[current]
		if !ok {
			continue
		}
		for _, parentID := range cur.Parents {
			if visited[parentID] {
				continue
			}
			visited[parentID] = true
			if parent, ok := byID[parentID]; ok {
				result = append(result, parent)
				queue = append(queue, parentID)
			}
		}
	}
	return result
}

func (m *BoxyardMeta) Roots() []*BoxMeta {
	var out []*BoxMeta
	for _, bm := range m.BoxMetas {
		if len(bm.Parents) == 0 {
			out = append(out, bm)
		}
	}
	return out
}

func (m *BoxyardMeta) Leaves() []*BoxMeta {
	parentIDs := map[string]bool{}
	for _, bm := range m.BoxMetas {
		for _, p := range bm.Parents {
			parentIDs[p] = true
		}
	}
	var out []*BoxMeta
	for _, bm := range m.BoxMetas {
		if !parentIDs[bm.BoxID()] {
			out = append(out, bm)
		}
	}
	return out
}

// WouldCreateCycle reports whether making proposedParentID a parent of childID
// would create a cycle.
func (m *BoxyardMeta) WouldCreateCycle(childID, proposedParentID string) bool {
	if childID == proposedParentID {
		return true
	}
	for _, a := range m.AncestorsOf(proposedParentID) {
		if a.BoxID() == childID {
			return true
		}
	}
	return false
}

// GroupConfigs returns the effective group configuration: those declared in the
// config, plus a default entry for every group a box claims membership of but
// that the config does not mention.
func GroupConfigs(cfg *config.Config, boxMetas []*BoxMeta) (map[string]*config.BoxGroupConfig, map[string]*config.VirtualBoxGroupConfig) {
	groups := make(map[string]*config.BoxGroupConfig, len(cfg.BoxGroups))
	for name, g := range cfg.BoxGroups {
		groups[name] = g
	}
	for _, bm := range boxMetas {
		for _, name := range bm.Groups {
			if _, ok := groups[name]; !ok {
				groups[name] = &config.BoxGroupConfig{BoxTitleMode: config.TitleIndexName}
			}
		}
	}
	return groups, cfg.VirtualBoxGroups
}
