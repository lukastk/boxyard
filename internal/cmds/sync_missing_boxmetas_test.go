package cmds

import (
	"context"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/rclone"
)

func TestFindRenamesReconcilesOnBoxID(t *testing.T) {
	meta := "/boxmeta.toml"
	remote := map[string]bool{
		"20240102_aaaaa__new-name" + meta: true,
		"20240103_bbbbb__same" + meta:     true,
	}
	local := map[string]bool{
		"20240102_aaaaa__old-name" + meta: true,
		"20240103_bbbbb__same" + meta:     true,
	}

	got, err := findRenames(remote, local, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("got %v, want exactly the renamed box", got)
	}
	if got[0] != [2]string{"20240102_aaaaa__old-name", "20240102_aaaaa__new-name"} {
		t.Fatalf("got %v", got[0])
	}
}

// Ambiguity is SKIPPED rather than guessed at: more than one directory for one
// box id means something is already wrong, and picking one arbitrarily could
// rename a box onto a name that is already taken.
func TestFindRenamesSkipsAmbiguity(t *testing.T) {
	meta := "/boxmeta.toml"
	cases := []struct {
		name          string
		remote, local map[string]bool
	}{
		{
			"two remote names for one id",
			map[string]bool{"20240102_aaaaa__a" + meta: true, "20240102_aaaaa__b" + meta: true},
			map[string]bool{"20240102_aaaaa__c" + meta: true},
		},
		{
			"two local names for one id",
			map[string]bool{"20240102_aaaaa__a" + meta: true},
			map[string]bool{"20240102_aaaaa__b" + meta: true, "20240102_aaaaa__c" + meta: true},
		},
		{
			"present only remotely — that is a MISSING box, not a rename",
			map[string]bool{"20240102_aaaaa__a" + meta: true},
			map[string]bool{},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := findRenames(tc.remote, tc.local, nil)
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != 0 {
				t.Fatalf("got %v, want none", got)
			}
		})
	}
}

func TestFindRenamesHonoursTheNameFilter(t *testing.T) {
	meta := "/boxmeta.toml"
	remote := map[string]bool{
		"20240102_aaaaa__new-a" + meta: true,
		"20240103_bbbbb__new-b" + meta: true,
	}
	local := map[string]bool{
		"20240102_aaaaa__old-a" + meta: true,
		"20240103_bbbbb__old-b" + meta: true,
	}

	// Either side of the pair may be named.
	for _, only := range [][]string{
		{"20240102_aaaaa__old-a"},
		{"20240102_aaaaa__new-a"},
	} {
		got, err := findRenames(remote, local, only)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != 1 || !strings.HasSuffix(got[0][0], "old-a") {
			t.Fatalf("filter %v gave %v", only, got)
		}
	}
}

func TestSyncMissingBoxMetasRejectsBothFilters(t *testing.T) {
	cfg := remoteYard(t)
	err := SyncMissingBoxMetas(context.Background(), cfg, &metaStore{moveRecorder: newMoveRecorder()},
		SyncMissingBoxMetasOptions{
			BoxIndexNames:    []string{"a"},
			StorageLocations: []string{"remote"},
		})
	if err == nil || !strings.Contains(err.Error(), "Cannot provide both") {
		t.Fatalf("want a refusal, got %v", err)
	}
}

// A `local` storage location IS the local store: there is nothing to discover,
// and listing it would be a wasted round trip on every pass.
func TestSyncMissingBoxMetasSkipsLocalStorageLocations(t *testing.T) {
	cfg := newTestYard(t) // only a local storage location
	s := &metaStore{moveRecorder: newMoveRecorder()}
	if err := SyncMissingBoxMetas(context.Background(), cfg, s, SyncMissingBoxMetasOptions{}); err != nil {
		t.Fatal(err)
	}
	if s.lsCalls != 0 {
		t.Fatalf("a local storage location was listed %d times", s.lsCalls)
	}
}

// metaStore adds the raw listing to the move-recording fake.
type metaStore struct {
	*moveRecorder
	lsCalls int
	entries map[string][]rclone.Entry
}

func (m *metaStore) Lsjson(_ context.Context, loc rclone.Location, _ rclone.LsjsonOptions) ([]rclone.Entry, bool, error) {
	m.lsCalls++
	entries := m.entries[loc.Remote+"\x00"+loc.Path]
	return entries, entries != nil, nil
}
