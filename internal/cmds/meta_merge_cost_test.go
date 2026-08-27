package cmds

import (
	"context"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// Turning `merge_diverged_boxmetas` on must not make an ordinary sync pass more
// expensive.
//
// The first version asked, before every META sync, whether a merge was needed —
// and that question is a status check, which makes TWO remote calls. On the
// largest yard in this fleet that is 1,180 extra round trips every 20 minutes,
// for boxes with nothing wrong with them. This project has been bitten by
// exactly that shape once already: the tombstone fetch was per-box, "587 per
// pass, per machine, every 20 minutes, which saturated the storage box's
// connection limit and was failing ~8 boxes per pass on three machines".
//
// The merge is attempted from the sync's FAILURE path instead, and by the time
// it runs the refusal has already established the status — so it must not ask
// the store anything before it knows there is something to merge.

// untouchableStore fails the test on ANY use. The embedded nil interface makes
// every method it does not define panic, and Cat — the one the merge needs —
// is defined only so the MetaMergeStore assertion succeeds and the test is
// exercising the real path rather than the can't-merge early-out.
type untouchableStore struct {
	SyncStore
	t *testing.T
}

func (u untouchableStore) Cat(ctx context.Context, remote, path string) (bool, string, error) {
	u.t.Error("the merge read the remote before it knew there was anything to merge")
	return false, "", nil
}

func (u untouchableStore) PathExists(ctx context.Context, remote, path string) (bool, bool, error) {
	u.t.Error("the merge asked the remote for a status it had already been told")
	return false, false, nil
}

func (u untouchableStore) ReadSyncRecord(ctx context.Context, remote, path string) (*models.SyncRecord, error) {
	u.t.Error("the merge re-read a sync record the refusal had already read")
	return nil, nil
}

func (u untouchableStore) Lsjson(ctx context.Context, loc rclone.Location, o rclone.LsjsonOptions) ([]rclone.Entry, bool, error) {
	u.t.Error("the merge listed the remote")
	return nil, false, nil
}

func TestTheMergeAsksTheRemoteNothingWhenThereIsNothingToMerge(t *testing.T) {
	cfg := newTestYard(t)
	cfg.StorageLocations["remote"] = &config.StorageConfig{
		StorageType: config.StorageRclone,
		StorePath:   "boxyard",
	}
	cfg.MergeDivergedBoxmetas = true

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{
		BoxName: "untroubled", StorageLocation: "remote",
	})
	if err != nil {
		t.Fatal(err)
	}
	bm, err := models.LoadBoxMeta(cfg, "remote", indexName)
	if err != nil {
		t.Fatal(err)
	}

	localPath, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	req := syncengine.HelperRequest{
		StatusRequest: syncengine.StatusRequest{
			LocalPath:            localPath,
			LocalSyncRecordPath:  bm.LocalSyncRecordPath(cfg, enums.PartMeta),
			Remote:               "remote",
			RemotePath:           "boxyard/boxes/" + indexName + "/boxmeta.toml",
			RemoteSyncRecordPath: "boxyard/sync_records/" + indexName + "/meta.rec",
		},
		Setting: enums.SyncCareful,
	}

	// No merge base: this box has never synced its META, which is the state
	// every box is in until one sync has run. The merge must decline without
	// touching the remote.
	merged, err := tryMergeDivergedBoxmeta(context.Background(), cfg,
		untouchableStore{t: t}, bm, req, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if merged {
		t.Error("a box with no merge base was reported as merged")
	}
}

func TestTheMergeIsInertWhenTheSettingIsOff(t *testing.T) {
	cfg := newTestYard(t)
	cfg.MergeDivergedBoxmetas = false
	bm := &models.BoxMeta{
		CreationTimestampUTC: "20260822_000000", BoxSubid: "aaaaa",
		Name: "a-box", StorageLocation: "remote", Groups: []string{}, Parents: []string{},
	}
	merged, err := tryMergeDivergedBoxmeta(context.Background(), cfg,
		untouchableStore{t: t}, bm, syncengine.HelperRequest{}, func(string, ...any) {})
	if err != nil || merged {
		t.Fatalf("merged=%v err=%v", merged, err)
	}
}
