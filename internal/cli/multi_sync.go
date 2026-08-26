package cli

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/perms"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/runner"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/lukastk/boxyard/internal/tombstones"
	"github.com/spf13/cobra"
	"golang.org/x/sys/unix"
)

// syncStat is one box's outcome, in the vocabulary the output uses.
type syncStat struct {
	Num     int
	Status  string
	Err     error
	Results map[enums.BoxPart]cmds.PartResult
}

func newMultiSyncCommand() *cobra.Command {
	var (
		boxIndexNames           []string
		storageLocations        []string
		maxConcurrent           int
		syncDirection           string
		syncSetting             string
		syncChoices             []string
		recentlyModifiedFirst   bool
		noRecentlyModifiedFirst bool
		refreshUserSymlinks     bool
		noRefreshSymlinks       bool
		showProgress            bool
		noShowProgress          bool
		noPrintSkipped          bool
		noNoPrintSkipped        bool
		softInterruption        bool
		noSoftInterruption      bool
	)

	cmd := &cobra.Command{
		Use:   "multi-sync",
		Short: "Sync multiple boxes",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			if noShowProgress {
				showProgress = false
			}
			if noNoPrintSkipped {
				noPrintSkipped = false
			}
			if noRecentlyModifiedFirst {
				recentlyModifiedFirst = false
			}
			if noSoftInterruption {
				softInterruption = false
			}
			direction, err := parseDirection(syncDirection)
			if err != nil {
				return err
			}
			setting := enums.SyncSetting(syncSetting) // validated at parse time
			parts, err := parseBoxParts(syncChoices)
			if err != nil {
				return err
			}
			reportParts := parts
			if reportParts == nil {
				reportParts = enums.AllBoxParts
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			if len(storageLocations) == 0 && len(boxIndexNames) == 0 {
				for name := range cfg.StorageLocations {
					storageLocations = append(storageLocations, name)
				}
			}
			for _, sl := range storageLocations {
				if _, ok := cfg.StorageLocations[sl]; !ok {
					fmt.Printf("Invalid storage location: [%s]\n", strings.Join(storageLocations, " "))
					os.Exit(1)
				}
			}
			if maxConcurrent == 0 {
				maxConcurrent = cfg.MaxConcurrentRcloneOps
			}

			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			boxes, err := selectMultiSyncBoxes(meta, storageLocations, boxIndexNames)
			if err != nil {
				return err
			}
			if recentlyModifiedFirst {
				sortByRecentlyModified(cfg, boxes)
			}

			client, err := rclone.New(cfg.RcloneConfigPath())
			if err != nil {
				return err
			}
			store := storage.New(client)
			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()

			// Fetch the tombstones ONCE, not once per box. Asked per box that
			// is one SFTP connection each — 587 per pass, per machine, every 20
			// minutes, which saturated the storage box's connection limit and
			// was failing ~8 boxes per pass on three machines.
			//
			// A failure here is NOT survivable by carrying on: without knowing
			// which boxes are tombstoned, syncing would resurrect a box another
			// machine deleted. So it fails, naming the location.
			tombstonedBySL, err := loadTombstonedIDs(ctx, cfg, store, boxes)
			if err != nil {
				return err
			}

			stats := make([]syncStat, len(boxes))
			var mu sync.Mutex
			tasks := make([]func(context.Context) (struct{}, error), len(boxes))
			for i := range boxes {
				num, bm := i, boxes[i]
				tasks[i] = func(ctx context.Context) (struct{}, error) {
					results, err := cmds.SyncBox(ctx, cfg, store, perms.Adapter{}, cmds.SyncBoxOptions{
						BoxIndexName:     bm.IndexName(),
						Direction:        direction,
						Setting:          setting,
						Choices:          parts,
						TombstonedBoxIDs: tombstonedBySL[bm.StorageLocation],
					})
					stat := syncStat{Num: num, Results: results}
					switch {
					case errors.Is(err, context.Canceled):
						stat.Status = "Interrupted"
					case err != nil:
						stat.Status, stat.Err = "Error", err
					default:
						stat.Status = classifySync(results)
					}
					mu.Lock()
					stats[num] = stat
					if showProgress {
						printBoxResult(stats[num], bm, len(boxes), reportParts, noPrintSkipped)
					}
					mu.Unlock()
					return struct{}{}, nil
				}
			}
			if _, err := runner.Throttle(ctx, maxConcurrent, 0, tasks); err != nil {
				return err
			}

			fmt.Println("Finished. Final results:")
			fmt.Println()
			fmt.Println()
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}

	f := cmd.Flags()
	f.StringArrayVarP(&boxIndexNames, "box", "r", nil, "The index names of the box, in the form.")
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil, "The storage locations to sync.")
	f.IntVarP(&maxConcurrent, "max-concurrent", "m", 0, "The maximum number of concurrent rclone operations.")
	enumVar(f, &syncDirection, "sync-direction", "", "", "The direction of the sync.", enums.SyncDirectionNames)
	enumVar(f, &syncSetting, "sync-setting", "", string(enums.SyncCareful), "The sync setting to use.", enums.SyncSettingNames)
	enumSliceVar(f, &syncChoices, "sync-choices", "c", "The parts of the box to sync.", enums.BoxPartNames)
	f.BoolVar(&recentlyModifiedFirst, "sync-recently-modified-first", false, "Sync boxes that have been recently modified first.")
	f.BoolVar(&noRecentlyModifiedFirst, "no-sync-recently-modified-first", false, "Do not sync recently modified boxes first.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&showProgress, "show-progress", true, "Show the progress of the sync.")
	f.BoolVar(&noShowProgress, "no-show-progress", false, "Do not show the progress of the sync.")
	f.BoolVar(&noPrintSkipped, "no-print-skipped", true, "Do not print boxes for which no syncs happened.")
	// The double negative is not a typo. typer derives the off-switch for a
	// bool option by prefixing "--no-", so a Python parameter already called
	// `no_print_skipped` yields `--no-no-print-skipped` — and that spelling,
	// ugly as it is, is what the CLI contract says. A friendlier
	// `--print-skipped` was here instead, which the Python rejects with exit
	// 2, so a script written against the Go would have broken on any machine
	// still running the Python.
	f.BoolVar(&noNoPrintSkipped, "no-no-print-skipped", false, "Print boxes for which no syncs happened.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

func selectMultiSyncBoxes(meta *models.BoxyardMeta, storageLocations, boxIndexNames []string) ([]*models.BoxMeta, error) {
	if len(boxIndexNames) == 0 {
		var out []*models.BoxMeta
		for _, bm := range meta.BoxMetas {
			if containsString(storageLocations, bm.StorageLocation) {
				out = append(out, bm)
			}
		}
		return out, nil
	}
	byIndex := meta.ByIndexName()
	out := make([]*models.BoxMeta, 0, len(boxIndexNames))
	for _, name := range boxIndexNames {
		bm, ok := byIndex[name]
		if !ok {
			fmt.Printf("Non-existent box: [%s]\n", strings.Join(boxIndexNames, " "))
			os.Exit(1)
		}
		out = append(out, bm)
	}
	return out, nil
}

// sortByRecentlyModified puts the boxes most recently touched first, so a pass
// that is interrupted has already done the ones most likely to matter.
func sortByRecentlyModified(cfg *config.Config, boxes []*models.BoxMeta) {
	modified := make(map[string]time.Time, len(boxes))
	a := storage.New(nil)
	for _, bm := range boxes {
		t, found, err := a.LocalLastModified(bm.LocalPath(cfg), nil)
		if err == nil && found {
			modified[bm.IndexName()] = t
		}
	}
	sort.SliceStable(boxes, func(i, j int) bool {
		return modified[boxes[i].IndexName()].After(modified[boxes[j].IndexName()])
	})
}

func loadTombstonedIDs(ctx context.Context, cfg *config.Config, s *storage.Adapter,
	boxes []*models.BoxMeta) (map[string]map[string]bool, error) {

	seen := map[string]bool{}
	var names []string
	for _, bm := range boxes {
		if !seen[bm.StorageLocation] {
			seen[bm.StorageLocation] = true
			names = append(names, bm.StorageLocation)
		}
	}
	sort.Strings(names)

	out := map[string]map[string]bool{}
	for _, name := range names {
		slConfig, ok := cfg.StorageLocations[name]
		if !ok {
			return nil, fmt.Errorf("storage location '%s' not found", name)
		}
		if slConfig.StorageType == config.StorageLocal {
			// A local store has no tombstones and needs no remote call.
			continue
		}
		ids, err := tombstones.ListBoxIDs(ctx, s, cfg, name)
		if err != nil {
			return nil, fmt.Errorf("could not list tombstones at '%s': %w", name, err)
		}
		out[name] = ids
	}
	return out, nil
}

// classifySync turns a box's parts into the single word the output shows.
//
// "Read-only" and "Local" are their own statuses rather than errors or
// successes: multi-sync runs every 1200s under supervisor, and a red line for a
// state that is working as designed would repeat ~72 times a day per machine.
func classifySync(results map[enums.BoxPart]cmds.PartResult) string {
	if len(results) == 0 {
		return "Success"
	}
	allLocal := true
	for _, r := range results {
		if r.Status.Condition == syncengine.WriteDenied {
			return "Read-only"
		}
		if r.Status.Condition != syncengine.LocalStorage {
			allLocal = false
		}
	}
	if allLocal {
		return "Local"
	}
	return "Success"
}

func printBoxResult(stat syncStat, bm *models.BoxMeta, total int,
	parts []enums.BoxPart, noPrintSkipped bool) {

	anySynced := false
	for _, part := range parts {
		if r, ok := stat.Results[part]; ok && r.Synced {
			anySynced = true
		}
	}
	if noPrintSkipped && (stat.Status == "Success" || stat.Status == "Local") && !anySynced {
		return
	}

	left := fmt.Sprintf("(%d/%d) %s", stat.Num+1, total, bm.IndexName())
	dots := terminalWidth() - len(left) - len(stat.Status) - 1 - 2
	if dots < 1 {
		dots = 1
	}
	fmt.Printf("%s %s %s\n", left, strings.Repeat(".", dots), stat.Status)

	if stat.Err != nil {
		fmt.Printf("    %s\n", stat.Err)
		return
	}
	cells := make([]string, 0, len(parts))
	for _, part := range parts {
		cell := "Skipped"
		if r, ok := stat.Results[part]; ok {
			switch {
			case r.Status.Condition == syncengine.WriteDenied:
				cell = "Write denied"
			case r.Synced:
				cell = "Synced"
			}
		}
		cells = append(cells, fmt.Sprintf("%s: %s", part, cell))
	}
	fmt.Printf("    %s\n", strings.Join(cells, ",    "))
}

// terminalWidth matches Python's shutil.get_terminal_size((80, 20)).columns,
// which is what the dot padding is measured against.
//
// The PRECEDENCE is Python's, and it matters: COLUMNS wins over the real
// terminal size, and the fallback is 80 — which is what the supervisor's
// non-interactive runs get, and therefore what the deployed log looks like.
func terminalWidth() int {
	if v := os.Getenv("COLUMNS"); v != "" {
		var w int
		if _, err := fmt.Sscanf(v, "%d", &w); err == nil && w > 0 {
			return w
		}
	}
	if ws, err := unix.IoctlGetWinsize(int(os.Stdout.Fd()), unix.TIOCGWINSZ); err == nil && ws.Col > 0 {
		return int(ws.Col)
	}
	return 80
}
