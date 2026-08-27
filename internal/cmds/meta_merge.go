package cmds

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// MetaMergeStore is what the merge needs beyond a sync: the ability to read
// the remote boxmeta.
//
// Asserted at the call site rather than demanded in SyncBox's signature. A
// store that cannot do it simply does not merge, which leaves today's refusal
// in place — widening every caller's interface for an off-by-default path
// would be the wrong trade.
type MetaMergeStore interface {
	Cat(ctx context.Context, remote, path string) (bool, string, error)
}

// tryMergeDivergedBoxmeta reconciles a boxmeta that BOTH sides have edited,
// before the sync sees it.
//
// DECLINING IS THE DEFAULT SHAPE of this function. It bails out, in order,
// when: the setting is off; the store cannot read a remote file; the status is
// not a conflict; there is no merge base; the remote copy cannot be read; the
// two sides changed the same scalar differently. Every one of those leaves the
// divergence exactly as it was, so the refusal that follows is unchanged for
// anything this cannot settle. None of it is a fallback that papers over a
// failure.
//
// On success it FORCE-PUSHES the merged boxmeta. That is a write today's code
// would refuse to make, and it is justified by what a merge is: the result
// CONTAINS the remote's content as well as this machine's, so the push adds
// and never discards. It is also why `merge_diverged_boxmetas` is off by
// default.
func tryMergeDivergedBoxmeta(
	ctx context.Context,
	cfg *config.Config,
	s SyncStore,
	bm *models.BoxMeta,
	req syncengine.HelperRequest,
	printf func(string, ...any),
) error {
	if !cfg.MergeDivergedBoxmetas {
		return nil
	}
	store, ok := s.(MetaMergeStore)
	if !ok {
		// A store that cannot read a remote file cannot merge. Not an error:
		// the sync proceeds and refuses exactly as it does today.
		return nil
	}

	status, err := syncengine.GetSyncStatus(ctx, s, req.StatusRequest)
	if err != nil {
		return err
	}
	if status.Condition != syncengine.Conflict {
		return nil
	}

	base, err := models.ReadMetaBase(cfg, bm)
	if err != nil {
		return err
	}
	if base == nil {
		// A box that has not synced its META since the base was introduced.
		// There is nothing to compute a delta against, and guessing one is the
		// thing this whole design exists to avoid.
		printf("Boxmeta has diverged and there is no merge base to reconcile it against; " +
			"leaving it for `boxyard doctor`.\n")
		return nil
	}

	exists, raw, err := store.Cat(ctx, req.Remote, req.RemotePath)
	if err != nil || !exists {
		return err
	}

	tmp, err := os.CreateTemp("", "boxyard-remote-meta-*.toml")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.WriteString(raw); err != nil {
		tmp.Close()
		return err
	}
	tmp.Close()

	identity := models.BoxIdentity{
		CreationTimestampUTC: bm.CreationTimestampUTC,
		BoxSubid:             bm.BoxSubid,
		Name:                 bm.Name,
		StorageLocation:      bm.StorageLocation,
	}
	remoteMeta, err := models.LoadBoxMetaFromPath(tmpName, identity)
	if err != nil {
		printf("Could not read the remote boxmeta to merge it: %v\n", err)
		return nil
	}

	// Re-read the LOCAL boxmeta from disk rather than trusting the registry
	// snapshot: the cache is a snapshot of the last refresh, and anything that
	// reached boxmeta.toml since would be silently dropped.
	localMeta, err := models.LoadBoxMeta(cfg, bm.StorageLocation, bm.IndexName())
	if err != nil {
		return err
	}

	merged, err := models.MergeBoxMetas(base, localMeta, remoteMeta)
	if err != nil {
		var conflict *models.MetaMergeConflict
		if errors.As(err, &conflict) {
			// The one case a human still has to settle. Say which field, and
			// leave everything alone.
			fmt.Printf("Boxmeta of '%s' has diverged and cannot be merged automatically: "+
				"both sides changed %s. `boxyard doctor` names the ways out.\n",
				bm.IndexName(), joinFields(conflict.Fields))
			return nil
		}
		return err
	}

	if err := merged.Save(cfg); err != nil {
		return err
	}
	printf("Merged the diverged boxmeta; pushing the result.\n")

	push := enums.DirectionPush
	pushReq := req
	pushReq.Direction = &push
	pushReq.Setting = enums.SyncForce
	if _, _, err := syncengine.Run(ctx, s, nil, pushReq); err != nil {
		return err
	}
	return models.RecordMetaBase(cfg, bm)
}

func joinFields(fields []string) string {
	out := ""
	for i, f := range fields {
		if i > 0 {
			out += ", "
		}
		out += f
	}
	return out
}
