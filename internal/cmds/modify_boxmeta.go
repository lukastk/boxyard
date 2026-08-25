package cmds

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
)

// NameConflictError means the modification would put two boxes with the same
// name into a group that requires unique names.
//
// Its own type because the callers that catch it — add-to-group, include —
// report it as a refusal with the conflicting names, not as a crash.
type NameConflictError struct{ Message string }

func (e *NameConflictError) Error() string { return e.Message }

// BoxMetaModifications names the fields to change. A nil pointer leaves the
// field alone.
//
// Python takes a dict of field name to value, which cannot be checked until it
// reaches the model; every real call site sets only `groups` or `parents`, so
// the Go version makes the field set explicit and a typo a compile error.
type BoxMetaModifications struct {
	Groups  *[]string
	Parents *[]string
}

// ModifyBoxMeta edits one box's boxmeta.toml.
//
// Ported from pts/mod/cmds/05_modify_boxmeta.pct.py. NOTE that the Python
// version syncs META first (a non-exported cell in its notebook does, and
// several callers sync before calling); this does not, because the callers own
// that decision and hiding a network round trip inside an "edit a file"
// function is the wrong layering.
func ModifyBoxMeta(cfg *config.Config, boxIndexName string, mods BoxMetaModifications) (*models.BoxMeta, error) {
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return nil, err
	}
	cached, ok := meta.ByIndexName()[boxIndexName]
	if !ok {
		return nil, fmt.Errorf("Box '%s' not found.", boxIndexName)
	}

	// The cache answers "does this box exist here?", but the object about to be
	// WRITTEN BACK is re-read from boxmeta.toml. Modifying the cached copy
	// instead is a lost update: boxyard_meta.json is a snapshot of the last
	// refresh, and anything that reached boxmeta.toml since — a META pull from
	// another machine among them — would be silently overwritten with older
	// values. That matters most for keys this version does not know, which a
	// stale cache would strip on an ordinary `add-to-group`.
	boxMeta, err := models.LoadBoxMeta(cfg, cached.StorageLocation, boxIndexName)
	if err != nil {
		return nil, err
	}

	modified := *boxMeta
	if mods.Groups != nil {
		modified.Groups = append([]string{}, *mods.Groups...)
	}
	if mods.Parents != nil {
		modified.Parents = append([]string{}, *mods.Parents...)
	}
	if err := modified.Validate(); err != nil {
		return nil, err
	}

	// The yard as it would look AFTER the change, which is what the group and
	// parent rules have to be checked against.
	after := make([]*models.BoxMeta, 0, len(meta.BoxMetas))
	for _, bm := range meta.BoxMetas {
		if bm.IndexName() == boxIndexName {
			continue
		}
		after = append(after, bm)
	}
	after = append(after, &modified)

	if err := checkGroups(cfg, &modified, after); err != nil {
		return nil, err
	}
	if mods.Parents != nil {
		if err := checkParents(cfg, &modified, after, boxIndexName); err != nil {
			return nil, err
		}
	}

	if err := modified.Save(cfg); err != nil {
		return nil, err
	}
	if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
		return nil, err
	}
	return &modified, nil
}

func checkGroups(cfg *config.Config, modified *models.BoxMeta, after []*models.BoxMeta) error {
	groupConfigs, virtualGroups := models.GroupConfigs(cfg, after)
	for _, g := range modified.Groups {
		if _, ok := virtualGroups[g]; ok {
			// A virtual group is a FILTER over the yard, so "adding" a box to
			// one is meaningless — the membership is computed, and the write
			// would be silently ignored on the next refresh.
			return fmt.Errorf("Cannot add a box to a virtual box group (virtual box group: '%s')", g)
		}
		groupConfig, ok := groupConfigs[g]
		if !ok || !groupConfig.UniqueBoxNames {
			continue
		}
		counts := map[string]int{}
		for _, bm := range after {
			for _, bg := range bm.Groups {
				if bg == g {
					counts[bm.Name]++
					break
				}
			}
		}
		var dupes []string
		for name, n := range counts {
			if n > 1 {
				dupes = append(dupes, fmt.Sprintf("'%s' (count: %d)", name, n))
			}
		}
		if len(dupes) > 0 {
			sort.Strings(dupes)
			return &NameConflictError{Message: fmt.Sprintf(
				"Error modifying box meta for '%s':\nBox is in group '%s' which requires unique names. "+
					"After the modification, the following name(s) appear multiple times in this group: %s.",
				modified.IndexName(), g, strings.Join(dupes, ", "))}
		}
	}
	return nil
}

func checkParents(cfg *config.Config, modified *models.BoxMeta, after []*models.BoxMeta, boxIndexName string) error {
	temp := &models.BoxyardMeta{BoxMetas: after}
	for _, parentID := range modified.Parents {
		if temp.WouldCreateCycle(modified.BoxID(), parentID) {
			return fmt.Errorf("Adding parent '%s' to box '%s' would create a cycle.", parentID, boxIndexName)
		}
	}
	if cfg.SingleParent && len(modified.Parents) > 1 {
		return fmt.Errorf("Config has single_parent=True but box '%s' would have %d parents.",
			boxIndexName, len(modified.Parents))
	}
	// A parent that is not here yet is a WARNING, not a refusal: boxmetas
	// arrive by sync, so naming a parent this machine has not pulled is a
	// normal thing to do and blocking it would make the order of operations
	// matter.
	byID := temp.ByBoxID()
	for _, parentID := range modified.Parents {
		if _, ok := byID[parentID]; !ok {
			fmt.Fprintf(os.Stderr,
				"Warning: parent '%s' not found locally. It may not be synced yet.\n", parentID)
		}
	}
	return nil
}
