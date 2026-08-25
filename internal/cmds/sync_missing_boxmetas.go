package cmds

import (
	"context"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// MetaSyncStore is what SyncMissingBoxMetas needs: the ordinary sync store,
// plus the raw listing (it needs a filtered, depth-limited one) and the remote
// rename RenameBox performs when adopting a name from another machine.
type MetaSyncStore interface {
	RenameStore
	Lsjson(ctx context.Context, loc rclone.Location, o rclone.LsjsonOptions) ([]rclone.Entry, bool, error)
}

// SyncMissingBoxMetasOptions mirrors the Python `sync_missing_boxmetas`
// signature.
type SyncMissingBoxMetasOptions struct {
	// BoxIndexNames and StorageLocations are mutually exclusive filters.
	BoxIndexNames    []string
	StorageLocations []string
	Verbose          bool
	Out              io.Writer
}

// SyncMissingBoxMetas fetches boxmetas that exist on a remote but not here.
//
// Ported from pts/mod/cmds/04_sync_missing_boxmetas.pct.py. This is how a box
// created on one machine becomes visible on the others.
func SyncMissingBoxMetas(ctx context.Context, cfg *config.Config, s MetaSyncStore, opts SyncMissingBoxMetasOptions) error {
	if opts.BoxIndexNames != nil && opts.StorageLocations != nil {
		return fmt.Errorf("Cannot provide both `box_index_names` and `storage_locations`.")
	}
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	names := make([]string, 0, len(cfg.StorageLocations))
	for name := range cfg.StorageLocations {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, slName := range names {
		if err := ctx.Err(); err != nil {
			return err
		}
		slConfig := cfg.StorageLocations[slName]
		if slConfig.StorageType == config.StorageLocal {
			// A local storage location IS the local store: there is nothing to
			// discover.
			continue
		}
		if opts.StorageLocations != nil && !containsStr(opts.StorageLocations, slName) {
			continue
		}
		if err := syncOneLocation(ctx, cfg, s, opts, slName, slConfig.StorePath, printf); err != nil {
			return err
		}
	}

	_, err := models.RefreshBoxyardMeta(cfg, false)
	return err
}

func syncOneLocation(ctx context.Context, cfg *config.Config, s MetaSyncStore,
	opts SyncMissingBoxMetasOptions, slName, storePath string, printf func(string, ...any)) error {

	boxesPath := path.Join(storePath, boxconst.RemoteBoxesRelPath)
	lsOpts := rclone.LsjsonOptions{
		FilesOnly: true,
		Recursive: true,
		MaxDepth:  2,
		Filter:    []string{"+ " + boxconst.BoxMetafileRelPath},
	}
	remoteEntries, _, err := s.Lsjson(ctx, rclone.Remote(slName, boxesPath), lsOpts)
	if err != nil {
		return err
	}
	localOpts := lsOpts
	localOpts.Filter = []string{"+ /" + boxconst.BoxMetafileRelPath}
	localEntries, _, err := s.Lsjson(ctx,
		rclone.Local(filepath.Join(cfg.LocalStorePath(), slName)), localOpts)
	if err != nil {
		return err
	}

	remotePaths := pathSet(remoteEntries)
	localPaths := pathSet(localEntries)

	// Reconcile on BOX ID, not on index name.
	//
	// A box's index name is `{box_id}__{name}`, and a rename changes only the
	// name half — the id is preserved. Diffing the raw paths therefore treats a
	// renamed box as a brand-new one, and since this pass only ever ADDS, every
	// other machine ends up with TWO registrations for the same box id: the
	// stale pre-rename name, which nothing removes, plus the new one fetched
	// here. That is what doctor's `duplicate-box-id` was reporting on three
	// machines at once.
	renames, err := findRenames(remotePaths, localPaths, opts.BoxIndexNames)
	if err != nil {
		return err
	}
	if len(renames) > 0 {
		// RenameBox resolves the box through the cached registry, so make sure
		// the cache reflects what is actually on disk before renaming.
		if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
			return err
		}
		for _, r := range renames {
			newName := strings.SplitN(r[1], "__", 2)[1]
			printf("Adopting remote name for '%s' -> '%s' (renamed on another machine).\n", r[0], r[1])
			if _, err := RenameBox(ctx, cfg, s, RenameBoxOptions{
				BoxIndexName: r[0], NewName: newName, Scope: enums.RenameLocal,
			}); err != nil {
				return err
			}
			// The local registration now matches the remote, so it is no longer
			// missing.
			for p := range localPaths {
				if firstSegment(p) == r[0] {
					delete(localPaths, p)
				}
			}
			localPaths[r[1]+"/"+boxconst.BoxMetafileRelPath] = true
		}
	}

	var missing []string
	for p := range remotePaths {
		if localPaths[p] {
			continue
		}
		if opts.BoxIndexNames != nil && !containsStr(opts.BoxIndexNames, firstSegment(p)) {
			continue
		}
		missing = append(missing, p)
	}
	sort.Strings(missing)

	if err := ctx.Err(); err != nil {
		return err
	}
	if len(missing) == 0 {
		printf("No missing boxmetas in '%s' to sync.\n", slName)
		return nil
	}

	printf("Syncing %d missing boxmetas from '%s'.\n", len(missing), slName)
	for _, p := range missing {
		printf("  - %s\n", p)
	}

	// One rclone sync with an explicit allow-list, rather than one per box: a
	// yard with hundreds of new boxes would otherwise open hundreds of
	// connections, which is the pathology the batched tombstone probe exists to
	// avoid elsewhere.
	filter := make([]string, 0, len(missing)+1)
	for _, p := range missing {
		filter = append(filter, "+ /"+p)
	}
	filter = append(filter, "- **")
	ok, _, stderr, err := s.Sync(ctx, syncengine.SyncOptions{
		Source: slName, SourcePath: boxesPath,
		Dest: "", DestPath: filepath.Join(cfg.LocalStorePath(), slName),
		Filter: filter,
	})
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf("failed to fetch missing boxmetas from '%s': %s", slName, stderr)
	}

	// Each fetched boxmeta needs its META sync record copied down too, or the
	// next status probe sees content with no record and calls it an error.
	for _, p := range missing {
		indexName := firstSegment(p)
		boxMeta, err := models.LoadBoxMeta(cfg, slName, indexName)
		if err != nil {
			return err
		}
		remoteRecordPath, err := boxMeta.RemoteSyncRecordPath(cfg, enums.PartMeta)
		if err != nil {
			return err
		}
		rec, err := s.ReadSyncRecord(ctx, slName, remoteRecordPath)
		if err != nil {
			return err
		}
		if rec == nil {
			// No record on the remote is not an error here: the box's boxmeta
			// arrived, and the next sync will write one. Inventing a record
			// would claim a sync that never happened.
			continue
		}
		if err := s.WriteSyncRecord(ctx, "", boxMeta.LocalSyncRecordPath(cfg, enums.PartMeta), *rec); err != nil {
			return err
		}
	}
	return nil
}

// findRenames pairs a local index name with the remote one carrying the same
// box id.
//
// Ambiguity is SKIPPED rather than guessed at: more than one directory for one
// id on either side means something is already wrong, and picking one
// arbitrarily could rename a box onto a name that is already taken.
func findRenames(remotePaths, localPaths map[string]bool, only []string) ([][2]string, error) {
	remoteByID, err := indexNamesByBoxID(remotePaths)
	if err != nil {
		return nil, err
	}
	localByID, err := indexNamesByBoxID(localPaths)
	if err != nil {
		return nil, err
	}

	var out [][2]string
	ids := make([]string, 0, len(remoteByID))
	for id := range remoteByID {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		remoteNames, localNames := remoteByID[id], localByID[id]
		if len(localNames) != 1 || len(remoteNames) != 1 {
			continue
		}
		// The remote is authoritative for names, so adopt it — exactly what
		// `boxyard sync-name --direction to_local` does.
		if remoteNames[0] != localNames[0] {
			if only != nil && !containsStr(only, localNames[0]) && !containsStr(only, remoteNames[0]) {
				continue
			}
			out = append(out, [2]string{localNames[0], remoteNames[0]})
		}
	}
	return out, nil
}

func indexNamesByBoxID(paths map[string]bool) (map[string][]string, error) {
	byID := map[string]map[string]bool{}
	for p := range paths {
		indexName := firstSegment(p)
		boxID, err := models.ExtractBoxID(indexName)
		if err != nil {
			// A directory that is not a box registration is not this pass's
			// problem; the registry refresh reports it.
			continue
		}
		if byID[boxID] == nil {
			byID[boxID] = map[string]bool{}
		}
		byID[boxID][indexName] = true
	}
	out := make(map[string][]string, len(byID))
	for id, names := range byID {
		list := make([]string, 0, len(names))
		for n := range names {
			list = append(list, n)
		}
		sort.Strings(list)
		out[id] = list
	}
	return out, nil
}

func pathSet(entries []rclone.Entry) map[string]bool {
	out := make(map[string]bool, len(entries))
	for _, e := range entries {
		out[e.Path] = true
	}
	return out
}

func firstSegment(p string) string {
	if i := strings.IndexByte(p, '/'); i >= 0 {
		return p[:i]
	}
	return p
}

func containsStr(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}
