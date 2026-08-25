package cmds

import (
	"context"
	"fmt"
	"io"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/remoteindex"
)

// SyncNameOptions mirrors the Python `sync_name` signature.
type SyncNameOptions struct {
	BoxIndexName string
	Direction    enums.SyncNameDirection
	Verbose      bool
	Out          io.Writer
}

// SyncName makes a box's local and remote names agree, in the direction asked
// for, and returns the resulting index name.
//
// Ported from pts/mod/cmds/11_sync_name.pct.py. A box's ID is the same on both
// sides; only the name half of the index name can diverge, which is what
// happens when one machine renames a box and another has not caught up.
func SyncName(ctx context.Context, cfg *config.Config, s RenameStore, opts SyncNameOptions) (string, error) {
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return "", fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}
	boxID := bm.BoxID()
	localName := bm.Name

	printf("Syncing name for box ID: %s\n", boxID)
	printf("Local name: %s\n", localName)

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	if slConfig.StorageType == config.StorageLocal {
		return "", fmt.Errorf("Cannot sync name for local storage locations.")
	}

	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, bm.StorageLocation, boxID)
	if err != nil {
		return "", err
	}
	if remoteIndexName == "" {
		return "", fmt.Errorf("Remote box not found for ID '%s'. Cannot sync name.", boxID)
	}
	_, remoteName, err := models.ParseIndexName(remoteIndexName)
	if err != nil {
		return "", err
	}
	printf("Remote name: %s\n", remoteName)

	var sourceName, targetName, actionDesc string
	var scope enums.RenameScope
	switch opts.Direction {
	case enums.NameToLocal:
		sourceName, targetName, actionDesc, scope = remoteName, localName, "remote -> local", enums.RenameLocal
	case enums.NameToRemote:
		sourceName, targetName, actionDesc, scope = localName, remoteName, "local -> remote", enums.RenameRemote
	default:
		return "", fmt.Errorf("Invalid direction: %s", opts.Direction)
	}

	if sourceName == targetName {
		printf("Names already match: '%s'. Nothing to do.\n", sourceName)
		return opts.BoxIndexName, nil
	}
	printf("Syncing name (%s): '%s' -> '%s'\n", actionDesc, targetName, sourceName)

	result, err := RenameBox(ctx, cfg, s, RenameBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		NewName:      sourceName,
		Scope:        scope,
		Verbose:      opts.Verbose,
		Out:          opts.Out,
	})
	if err != nil {
		return "", err
	}
	printf("Name sync complete. Result index: %s\n", result)
	return result, nil
}
