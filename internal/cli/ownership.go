package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/perms"
	"github.com/spf13/cobra"
)

// ownerInfo is the `owner -o json` payload. Field order is the key order
// Python's json.dumps produces.
type ownerInfo struct {
	IndexName    string  `json:"index_name"`
	WriteOwner   *string `json:"write_owner"`
	ThisMachine  *string `json:"this_machine"`
	IncludedHere bool    `json:"included_here"`
	WritableHere bool    `json:"writable_here"`
}

func newClaimCommand() *cobra.Command {
	var (
		sel         boxSelectorFlags
		steal       bool
		allIncluded bool
		yes         bool
	)

	cmd := &cobra.Command{
		Use:   "claim",
		Short: "Make this machine the write owner of a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			if allIncluded {
				return claimAllIncluded(cfg, steal)
			}
			// --yes only suppresses the --steal confirmation prompt, which is
			// not ported: nothing here prompts, so the flag is a no-op that
			// exists for surface parity.
			_ = yes

			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			_, err = cmds.ClaimBox(ctx, cfg, store, perms.Adapter{}, cmds.ClaimBoxOptions{
				BoxIndexName: indexName, Steal: steal, Verbose: true, Out: os.Stdout,
			})
			return err
		},
	}

	sel.register(cmd, selectorSpec{Noun: "claim", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.BoolVar(&steal, "steal", false, "Take the box from the machine that currently owns it.")
	f.BoolVar(&allIncluded, "all-included", false, "Claim every box included on this machine that has no owner.")
	f.BoolVarP(&yes, "yes", "y", false, "Skip the confirmation prompt for --steal.")
	return cmd
}

// claimAllIncluded is the bulk pass used to adopt ownership across a yard.
func claimAllIncluded(cfg *config.Config, steal bool) error {
	if steal {
		// A bulk pass must NEVER take boxes from other machines. Those are
		// claimed one at a time, so each decision is visible.
		fmt.Fprintln(os.Stderr,
			"--all-included and --steal cannot be combined: a bulk pass must never take "+
				"boxes from other machines. Claim those one at a time, so each decision is visible.")
		os.Exit(1)
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	// Boxes in a LOCAL storage location are SKIPPED rather than attempted and
	// refused: no other machine can reach them, so there is nothing to
	// coordinate, and listing every one as "needs a decision" would bury the
	// boxes that genuinely do.
	var here []*models.BoxMeta
	for _, bm := range meta.BoxMetas {
		if !bm.CheckIncluded(cfg) {
			continue
		}
		slConfig, err := bm.StorageLocationConfig(cfg)
		if err != nil {
			return err
		}
		if slConfig.StorageType != config.StorageRclone {
			continue
		}
		here = append(here, bm)
	}

	var candidates, ownedElsewhere []*models.BoxMeta
	for _, bm := range here {
		switch {
		case bm.WriteOwner == "":
			candidates = append(candidates, bm)
		case bm.WriteOwner != cfg.MachineName:
			// Boxes included HERE but owned by ANOTHER machine are the whole
			// reason to run this as a pass: they are the boxes genuinely on two
			// machines at once, which nothing could enumerate before. They are
			// not claimed — that would be a silent mass steal — but they are
			// LISTED, because a migration that quietly skipped them would leave
			// exactly the risk it was run to find.
			ownedElsewhere = append(ownedElsewhere, bm)
		}
	}
	if len(candidates) == 0 && len(ownedElsewhere) == 0 {
		fmt.Println("No unowned included boxes to claim.")
		return nil
	}
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].IndexName() < candidates[j].IndexName()
	})

	fmt.Printf("Claiming %d unowned box(es) included here.\n", len(candidates))
	store, err := newStore(cfg)
	if err != nil {
		return err
	}
	ctx, stop := maybeSoftInterrupt(true)
	defer stop()

	var failures []string
	for i, bm := range candidates {
		fmt.Printf("[%d/%d] %s\n", i+1, len(candidates), bm.IndexName())
		if _, err := cmds.ClaimBox(ctx, cfg, store, perms.Adapter{}, cmds.ClaimBoxOptions{
			BoxIndexName: bm.IndexName(),
		}); err != nil {
			fmt.Fprintf(os.Stderr, "  %v\n", err)
			failures = append(failures, bm.IndexName())
		}
	}
	if len(ownedElsewhere) > 0 {
		fmt.Printf("\n%d box(es) included here are owned by another machine:\n", len(ownedElsewhere))
		for _, bm := range ownedElsewhere {
			fmt.Printf("  %s (owned by '%s')\n", bm.IndexName(), bm.WriteOwner)
		}
	}
	if len(failures) > 0 {
		fmt.Fprintf(os.Stderr, "\n%d box(es) could not be claimed: %s\n",
			len(failures), strings.Join(failures, ", "))
		os.Exit(1)
	}
	return nil
}

func newReleaseCommand() *cobra.Command {
	var sel boxSelectorFlags

	cmd := &cobra.Command{
		Use:   "release",
		Short: "Give up this machine's write ownership of a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			return cmds.ReleaseBox(ctx, cfg, store, perms.Adapter{}, cmds.ReleaseBoxOptions{
				BoxIndexName: indexName, Verbose: true, Out: os.Stdout,
			})
		},
	}
	sel.register(cmd, selectorSpec{Noun: "release", MatchCaseShort: "c"})
	return cmd
}

func newOwnerCommand() *cobra.Command {
	var (
		sel          boxSelectorFlags
		outputFormat string
	)

	cmd := &cobra.Command{
		Use:   "owner",
		Short: "Show which machine may push a box's DATA",
		RunE: func(cmd *cobra.Command, args []string) error {
			if outputFormat != "text" && outputFormat != "json" {
				return &usageError{err: fmt.Errorf("invalid output format: %q", outputFormat)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			bm, ok := meta.ByIndexName()[indexName]
			if !ok {
				fmt.Fprintf(os.Stderr, "Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			info := ownerInfo{
				IndexName:    indexName,
				IncludedHere: bm.CheckIncluded(cfg),
				WritableHere: bm.WriteOwner == "" || bm.WriteOwner == cfg.MachineName,
			}
			// null, not "": an unowned box and an unnamed machine are both
			// absences in the JSON, and a shell caller testing `.write_owner ==
			// null` must keep working.
			if bm.WriteOwner != "" {
				owner := bm.WriteOwner
				info.WriteOwner = &owner
			}
			if cfg.MachineName != "" {
				machine := cfg.MachineName
				info.ThisMachine = &machine
			}

			if outputFormat == "json" {
				out, err := json.MarshalIndent(info, "", "  ")
				if err != nil {
					return err
				}
				fmt.Println(string(out))
				return nil
			}

			switch {
			case bm.WriteOwner == "":
				fmt.Printf("'%s' has no write owner, so any machine may push it "+
					"(the behaviour boxyard has always had).\n", indexName)
			case bm.WriteOwner == cfg.MachineName:
				fmt.Printf("'%s' is owned by this machine (%s).\n", indexName, bm.WriteOwner)
			default:
				machine := cfg.MachineName
				if machine == "" {
					machine = "unnamed"
				}
				fmt.Printf("'%s' is owned by '%s'. This machine (%s) pulls it but never pushes it.\n",
					indexName, bm.WriteOwner, machine)
			}
			return nil
		},
	}
	sel.register(cmd, selectorSpec{Noun: "inspect", MatchCaseShort: "c"})
	cmd.Flags().StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	return cmd
}

func newDiscardLocalCommand() *cobra.Command {
	var (
		sel      boxSelectorFlags
		yes      bool
		progress bool
	)

	cmd := &cobra.Command{
		Use:   "discard-local",
		Short: "Throw away this machine's local changes and take the remote copy",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			// --yes suppresses a confirmation prompt that is not ported; the
			// flag exists for surface parity.
			_ = yes

			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			_, err = cmds.DiscardLocal(ctx, cfg, store, perms.Adapter{}, cmds.DiscardLocalOptions{
				BoxIndexName: indexName, ShowRcloneProgress: progress,
				Verbose: true, Out: os.Stdout,
			})
			return err
		},
	}
	sel.register(cmd, selectorSpec{Noun: "discard", MatchCaseShort: "c"})
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip the confirmation prompt.")
	cmd.Flags().BoolVar(&progress, "progress", false, "Show rclone progress.")
	return cmd
}
