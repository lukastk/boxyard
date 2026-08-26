package doctor

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/strict"
)

// legacyBoxSubidRe is the subid format used by boxes created before the current
// config conventions. A long-lived yard is full of them, so validity cannot be
// derived from the config alone.
var legacyBoxSubidRe = regexp.MustCompile(`^[A-Za-z0-9]{5}$`)

var (
	dateOnlyRe    = regexp.MustCompile(`^\d{8}$`)
	dateAndTimeRe = regexp.MustCompile(`^\d{8}_\d{6}$`)
)

// IsValidIndexName reports whether s parses as `<timestamp>_<subid>__<name>`.
//
// BOTH timestamp formats are accepted regardless of the configured one: a yard
// commonly contains boxes created under older configs, and calling those
// malformed would bury the real findings.
func IsValidIndexName(s, subidCharacterSet string, subidLength int) bool {
	boxID, name, found := strings.Cut(s, "__")
	if !found || name == "" {
		return false
	}
	parts := strings.Split(boxID, "_")
	var timestampStr, subid, layout string
	switch len(parts) {
	case 2:
		timestampStr, subid = parts[0], parts[1]
		if !dateOnlyRe.MatchString(timestampStr) {
			return false
		}
		layout = boxconst.BoxTimestampFormatDateOnly
	case 3:
		timestampStr, subid = parts[0]+"_"+parts[1], parts[2]
		if !dateAndTimeRe.MatchString(timestampStr) {
			return false
		}
		layout = boxconst.BoxTimestampFormat
	default:
		return false
	}
	if _, err := time.Parse(layout, timestampStr); err != nil {
		return false
	}
	matchesConfig := len([]rune(subid)) == subidLength
	if matchesConfig {
		for _, c := range subid {
			if !strings.ContainsRune(subidCharacterSet, c) {
				matchesConfig = false
				break
			}
		}
	}
	return matchesConfig || legacyBoxSubidRe.MatchString(subid)
}

func checkUserBoxes(cfg *config.Config, report *Report, sc *scan) {
	entries, err := os.ReadDir(cfg.UserBoxesPath)
	if err != nil {
		return
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })

	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		path := filepath.Join(cfg.UserBoxesPath, entry.Name())
		if !entry.IsDir() {
			report.add("unregistered-folder",
				fmt.Sprintf("Stray file in user boxes path: '%s'", path),
				fmt.Sprintf("Only box directories belong in '%s'; move the file into a box or delete it.", cfg.UserBoxesPath),
				Field{"path", path})
			continue
		}
		if !sc.registeredIndexNames[entry.Name()] {
			report.add("unregistered-folder",
				fmt.Sprintf("Directory '%s' in '%s' is not a registered box", entry.Name(), cfg.UserBoxesPath),
				fmt.Sprintf("Register it with `boxyard new --from '%s' -n <name>` (moves it into a new box), or move it out of '%s'.", path, cfg.UserBoxesPath),
				Field{"path", path})
		}
	}
	// A second pass, as in the Python: the two checks report independently, so
	// a badly named directory shows up under both.
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") || !entry.IsDir() {
			continue
		}
		if !IsValidIndexName(entry.Name(), cfg.BoxSubidCharacterSet, cfg.BoxSubidLength) {
			report.add("malformed-name",
				fmt.Sprintf("Directory name '%s' does not parse as an index name '<timestamp>_<subid>__<name>'", entry.Name()),
				"Boxes must be created via `boxyard new`, which generates the index name; rename/move the folder or register it with `boxyard new --from <path>`.",
				Field{"path", filepath.Join(cfg.UserBoxesPath, entry.Name())})
		}
	}
}

func checkDuplicateBoxIDs(cfg *config.Config, report *Report, sc *scan) {
	byID := map[string][]*models.BoxMeta{}
	var order []string
	for _, bm := range sc.boxMetas {
		id := bm.BoxID()
		if _, seen := byID[id]; !seen {
			order = append(order, id)
		}
		byID[id] = append(byID[id], bm)
	}
	sort.Strings(order)
	for _, id := range order {
		bms := byID[id]
		if len(bms) < 2 {
			continue
		}
		locations := make([]string, len(bms))
		for i, bm := range bms {
			locations[i] = fmt.Sprintf("'%s/%s'", bm.StorageLocation, bm.IndexName())
		}
		report.add("duplicate-box-id",
			fmt.Sprintf("Box id '%s' is registered %d times: %s", id, len(bms), strings.Join(locations, ", ")),
			// The hint must never suggest `delete`: it purges the remote and
			// writes a tombstone keyed by box id, so following it would destroy
			// BOTH boxes. That is a real scar, not a hypothetical.
			"Box ids must be unique. This usually means the box was RENAMED on "+
				"another machine: `sync-missing-meta` fetched the new name while the "+
				"old registration stayed behind. The remote's name is authoritative "+
				"— check it with `boxyard copy`/`rclone lsf` or on the machine that "+
				"owns the box, then remove the registration whose name the remote "+
				"does not have. Do NOT re-create the box; that would mint a new id.",
			Field{"box_id", id})
	}
}

const staleCacheHint = "Run `boxyard create-user-symlinks` (or any mutating boxyard command) to regenerate it."

func checkStaleCache(cfg *config.Config, report *Report, sc *scan) {
	metaPath := cfg.BoxyardMetaPath()
	raw, err := os.ReadFile(metaPath)
	if os.IsNotExist(err) {
		report.add("stale-cache",
			fmt.Sprintf("Cache file '%s' does not exist", metaPath),
			staleCacheHint, Field{"path", metaPath})
		return
	}
	if err != nil {
		report.add("stale-cache",
			fmt.Sprintf("Cache file '%s' fails to parse: %v", metaPath, err),
			staleCacheHint, Field{"path", metaPath})
		return
	}
	var cached models.BoxyardMeta
	if err := strict.UnmarshalJSON(raw, &cached); err != nil {
		report.add("stale-cache",
			fmt.Sprintf("Cache file '%s' fails to parse: %v", metaPath, err),
			staleCacheHint, Field{"path", metaPath})
		return
	}

	type key struct{ sl, index string }
	cachedByKey := map[key]*models.BoxMeta{}
	for _, bm := range cached.BoxMetas {
		bm.NormalizeSlices()
		cachedByKey[key{bm.StorageLocation, bm.IndexName()}] = bm
	}
	diskByKey := map[key]*models.BoxMeta{}
	for _, bm := range sc.boxMetas {
		diskByKey[key{bm.StorageLocation, bm.IndexName()}] = bm
	}

	var keys []key
	seen := map[key]bool{}
	for k := range cachedByKey {
		if !seen[k] {
			keys = append(keys, k)
			seen[k] = true
		}
	}
	for k := range diskByKey {
		if !seen[k] {
			keys = append(keys, k)
			seen[k] = true
		}
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].sl != keys[j].sl {
			return keys[i].sl < keys[j].sl
		}
		return keys[i].index < keys[j].index
	})

	for _, k := range keys {
		c, inCache := cachedByKey[k]
		d, onDisk := diskByKey[k]
		switch {
		case inCache && !onDisk:
			report.add("stale-cache",
				fmt.Sprintf("Cache contains '%s/%s' but there is no such registration in the local store", k.sl, k.index),
				staleCacheHint, Field{"storage_location", k.sl}, Field{"index_name", k.index})
		case !inCache && onDisk:
			report.add("stale-cache",
				fmt.Sprintf("Registration '%s/%s' is missing from the cache", k.sl, k.index),
				staleCacheHint, Field{"storage_location", k.sl}, Field{"index_name", k.index})
		case !sameBoxMeta(c, d):
			report.add("stale-cache",
				fmt.Sprintf("Cache entry for '%s/%s' is out of date", k.sl, k.index),
				staleCacheHint, Field{"storage_location", k.sl}, Field{"index_name", k.index})
		}
	}
}

func sameBoxMeta(a, b *models.BoxMeta) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Name == b.Name &&
		a.StorageLocation == b.StorageLocation &&
		a.CreatorHostname == b.CreatorHostname &&
		a.WriteOwner == b.WriteOwner &&
		equalStrings(a.Groups, b.Groups) &&
		equalStrings(a.Parents, b.Parents)
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func checkGroupTree(cfg *config.Config, report *Report) {
	root := cfg.UserBoxGroupsPath
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || path == root {
			return nil
		}
		info, lerr := os.Lstat(path)
		if lerr != nil {
			return nil
		}
		if info.Mode()&os.ModeSymlink != 0 {
			if _, err := os.Stat(path); err != nil {
				target, _ := os.Readlink(path)
				report.add("dangling-symlinks",
					fmt.Sprintf("Symlink '%s' points to a non-existent target '%s'", path, target),
					"Run `boxyard create-user-symlinks` to rebuild the group symlinks.",
					Field{"path", path})
			}
			return nil
		}
		if !info.IsDir() {
			report.add("group-tree-debris",
				fmt.Sprintf("'%s' in the user box groups path is a real file, not a symlink", path),
				"`boxyard create-user-symlinks` (called by most mutating commands) refuses to run while real files are in the group tree; move or delete the file.",
				Field{"path", path})
		}
		return nil
	})
}

func checkSyncRecords(cfg *config.Config, report *Report, sc *scan) {
	recordsRoot := filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath)
	entries, err := os.ReadDir(recordsRoot)
	if err != nil {
		return
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		path := filepath.Join(recordsRoot, entry.Name())
		if !sc.registeredIndexNames[entry.Name()] && !sc.problemIndexNames[entry.Name()] {
			report.add("orphaned-sync-records",
				fmt.Sprintf("Sync records exist for '%s' but there is no such registration in the local store", entry.Name()),
				"Left over from a deleted or renamed box; delete the directory if the box is really gone.",
				Field{"path", path})
			continue
		}
		recs, err := os.ReadDir(path)
		if err != nil {
			continue
		}
		sort.Slice(recs, func(i, j int) bool { return recs[i].Name() < recs[j].Name() })
		for _, rec := range recs {
			if !strings.HasSuffix(rec.Name(), ".rec") {
				continue
			}
			recPath := filepath.Join(path, rec.Name())
			raw, err := os.ReadFile(recPath)
			if err != nil {
				continue
			}
			parsed, err := models.UnmarshalSyncRecord(raw)
			if err != nil {
				report.add("interrupted-sync",
					fmt.Sprintf("Sync record '%s' fails to parse: %v", recPath, err),
					fmt.Sprintf("Inspect the box with `boxyard box-status -r '%s'`; a fresh `boxyard sync` rewrites the record.", entry.Name()),
					Field{"path", recPath}, Field{"index_name", entry.Name()})
				continue
			}
			if !parsed.SyncComplete {
				stem := strings.TrimSuffix(rec.Name(), ".rec")
				report.add("interrupted-sync",
					fmt.Sprintf("A %s sync of '%s' was interrupted and never completed (record from host '%s' at %s)",
						stem, entry.Name(), parsed.SyncerHostname, parsed.Timestamp),
					fmt.Sprintf("The local copy may be incomplete. Inspect with `boxyard box-status -r '%s'` and re-run `boxyard sync -r '%s'` to recover.",
						entry.Name(), entry.Name()),
					Field{"path", recPath}, Field{"index_name", entry.Name()})
			}
		}
	}
}

func checkUnknownStorageLocations(cfg *config.Config, report *Report) {
	entries, err := os.ReadDir(cfg.LocalStorePath())
	if err == nil {
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			path := filepath.Join(cfg.LocalStorePath(), entry.Name())
			info, lerr := os.Stat(path)
			if lerr != nil || !info.IsDir() {
				report.add("unknown-storage-location",
					fmt.Sprintf("Stray file in the local store root: '%s'", path),
					"Only per-storage-location directories belong in the local store root; move or delete the file.",
					Field{"path", path})
				continue
			}
			if _, ok := cfg.StorageLocations[entry.Name()]; !ok {
				report.add("unknown-storage-location",
					fmt.Sprintf("Local store contains '%s' but no such storage location is configured", entry.Name()),
					"Left over from a removed or renamed storage location; delete the directory, or re-add the storage location to the config.",
					Field{"path", path})
			}
		}
	}

	indexes, err := os.ReadDir(cfg.RemoteIndexesPath())
	if err != nil {
		return
	}
	sort.Slice(indexes, func(i, j int) bool { return indexes[i].Name() < indexes[j].Name() })
	for _, entry := range indexes {
		name := strings.TrimSuffix(entry.Name(), ".json")
		if _, ok := cfg.StorageLocations[name]; !ok {
			report.add("unknown-storage-location",
				fmt.Sprintf("Remote index cache '%s' matches no configured storage location", entry.Name()),
				"Left over from a removed or renamed storage location; delete the file.",
				Field{"path", filepath.Join(cfg.RemoteIndexesPath(), entry.Name())})
		}
	}
}

func checkTreeOrphans(report *Report, sc *scan) {
	known := map[string]bool{}
	for _, bm := range sc.boxMetas {
		known[bm.BoxID()] = true
	}
	for _, bm := range sc.boxMetas {
		for _, parentID := range bm.Parents {
			if known[parentID] {
				continue
			}
			report.add("tree-orphans",
				fmt.Sprintf("Box '%s' references unknown parent box id '%s'", bm.IndexName(), parentID),
				"Fetch missing metas with `boxyard sync-missing-meta`, or drop the stale parent with `boxyard remove-parent`.",
				Field{"index_name", bm.IndexName()}, Field{"parent_box_id", parentID})
		}
	}
}

// checkUnpushedMetaEdits reports a boxmeta that differs from the merge base —
// the copy this machine last agreed with the remote about — with no push since.
//
// On its own that is an ordinary pending edit, not a fault: `add-to-group`,
// `set-parent` and friends do not push unless asked (`--sync-after`). The
// point is the TIMING. While the edit sits unpushed it is one push by any
// other machine away from a two-sided divergence, and a two-sided divergence
// is a dead end — sync refuses, and nothing but a human picking a winner per
// box resolves it.
//
// That is not hypothetical. On 2026-08-25, forty-four boxes on macbook were
// given an `archived` or `dormant` group locally; over the same afternoon the
// other machines ran the ownership claim sweep, which writes `write_owner`
// into boxmeta.toml and pushes. Every one of the forty-four became a conflict,
// they stopped propagating their groups entirely, and every machine except
// macbook reported "all checks passed" throughout.
//
// Purely local and free — it compares two files already on disk — and it says
// nothing about a box with no base yet. That is an absence, not a fault: a box
// whose META has not synced since the base was introduced has nothing to be
// compared against, and reporting it would flag every box on every machine on
// the day of the upgrade.
func checkUnpushedMetaEdits(cfg *config.Config, report *Report, sc *scan) error {
	for _, bm := range sc.boxMetas {
		base, err := models.ReadMetaBase(cfg, bm)
		if err != nil {
			return err
		}
		if base == nil {
			continue
		}

		// Compare the FIELDS, not the file bytes. A boxmeta rewritten with the
		// same content — reordered keys, a trailing newline — is not an edit,
		// and reporting it would train the reader to ignore this check.
		var changed []string
		if !equalStrings(bm.Groups, base.Groups) {
			changed = append(changed, "groups")
		}
		if !equalStrings(bm.Parents, base.Parents) {
			changed = append(changed, "parents")
		}
		if bm.WriteOwner != base.WriteOwner {
			changed = append(changed, "write_owner")
		}
		if len(changed) == 0 {
			continue
		}

		report.add("unpushed-meta-edit",
			fmt.Sprintf("Box '%s' has local metadata changes (%s) that have not been pushed",
				bm.IndexName(), strings.Join(changed, ", ")),
			fmt.Sprintf("Harmless until another machine pushes this box's META, at which point "+
				"it becomes a divergence that sync refuses. Push it with `boxyard sync -r '%s' "+
				"--sync-choices meta`.", bm.IndexName()),
			Field{"index_name", bm.IndexName()},
			Field{"changed_fields", changed},
			Field{"storage_location", bm.StorageLocation})
	}
	return nil
}
