package cmds

import (
	"context"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/rclone"
)

// `sync_before_new_box` fetches every remote boxmeta BEFORE an id is minted,
// so an id already taken on the remote is seen rather than collided with. The
// setting is off everywhere, which is exactly why it needs a test: the Python
// shipped it broken for months (an import of a function that had been renamed,
// plus an `asyncio.get_event_loop()` that raises on 3.14) and nothing noticed,
// because nothing ever ran the branch.

// partialMetaStore implements only Lsjson. Every other method of
// MetaSyncStore is inherited from the embedded nil interface, so a code path
// that needs one panics loudly instead of quietly getting a zero value.
type partialMetaStore struct {
	MetaSyncStore
	listed []string
}

func (p *partialMetaStore) Lsjson(_ context.Context, loc rclone.Location, _ rclone.LsjsonOptions) ([]rclone.Entry, bool, error) {
	p.listed = append(p.listed, loc.Spec())
	return nil, true, nil
}

func TestNewBoxSyncsBoxMetasFirstWhenEnabled(t *testing.T) {
	// remoteYard adds an rclone-typed location: a local one IS the local
	// store, so the discovery pass skips it and the setting would do nothing.
	cfg := remoteYard(t)
	cfg.SyncBeforeNewBox = true

	store := &partialMetaStore{}
	var out strings.Builder
	indexName, err := NewBox(context.Background(), cfg, store, NewBoxOptions{
		BoxName: "checked", StorageLocation: "remote", Verbose: true, Out: &out,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(store.listed) == 0 {
		t.Fatal("the pre-flight boxmeta sync never ran, so the id was minted without checking the remote")
	}
	if !strings.Contains(out.String(), "Syncing boxmetas before creating new box...") {
		t.Errorf("no progress line for the pre-flight sync: %q", out.String())
	}
	if !strings.HasSuffix(indexName, "__checked") {
		t.Errorf("index name = %q", indexName)
	}
}

func TestNewBoxDoesNotSyncWhenDisabled(t *testing.T) {
	cfg := remoteYard(t)
	store := &partialMetaStore{}
	if _, err := NewBox(context.Background(), cfg, store, NewBoxOptions{
		BoxName: "plain", StorageLocation: "remote",
	}); err != nil {
		t.Fatal(err)
	}
	if len(store.listed) != 0 {
		t.Errorf("the remote was listed with the setting off: %v", store.listed)
	}
}

func TestNewBoxRefusesWhenEnabledWithoutAStore(t *testing.T) {
	cfg := remoteYard(t)
	cfg.SyncBeforeNewBox = true
	// Silently minting the id anyway would remove the only guarantee the
	// setting exists to provide, and it would do it invisibly.
	_, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "unchecked"})
	if err == nil {
		t.Fatal("expected a refusal, got a box")
	}
	if !strings.Contains(err.Error(), "sync_before_new_box") {
		t.Errorf("the error does not name the setting: %v", err)
	}
}
