package cmds

import (
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

// `--from` moved the tree in with os.Rename, which cannot cross a filesystem
// boundary, so staging anywhere not on user_boxes_path's filesystem — /tmp
// where it is tmpfs, /dev/shm, an external disk — failed with EXDEV. The same
// command worked on mymain and failed on ideapad purely because of how each
// mounts /tmp.

// otherFilesystem returns a writable directory on a DIFFERENT filesystem from
// target, or "" if there is none to test against.
func otherFilesystem(t *testing.T, target string) string {
	t.Helper()
	targetInfo, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	targetDev := deviceOf(targetInfo)
	for _, c := range []string{"/dev/shm", filepath.Join("/run/user", itoaU(os.Getuid()))} {
		info, err := os.Stat(c)
		if err != nil {
			continue
		}
		if unix := deviceOf(info); unix != 0 && unix != targetDev {
			if f, err := os.CreateTemp(c, "boxyard-writable-"); err == nil {
				f.Close()
				os.Remove(f.Name())
				return c
			}
		}
	}
	return ""
}

func TestNewBoxFromAcrossAFilesystemBoundary(t *testing.T) {
	cfg := newTestYard(t)

	other := otherFilesystem(t, cfg.UserBoxesPath)
	if other == "" {
		t.Skip("no second filesystem available to stage on")
	}
	staged, err := os.MkdirTemp(other, "boxyard-crossdev-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(staged)
	if err := os.MkdirAll(filepath.Join(staged, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(staged, "sub", "payload.txt"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{
		BoxName: "crossdev", FromPath: staged,
	})
	if err != nil {
		t.Fatalf("--from across filesystems: %v", err)
	}

	bm, err := models.LoadBoxMeta(cfg, "local", indexName)
	if err != nil {
		t.Fatal(err)
	}
	data, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(data, "sub", "payload.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "hi\n" {
		t.Errorf("payload = %q", got)
	}
	// MOVED, not copied: leaving the source behind is what --copy is for, and
	// paying a second full copy is the cost --from exists to avoid.
	if _, err := os.Stat(staged); !os.IsNotExist(err) {
		t.Error("the source survived, so this was a copy")
	}
}

// deviceOf returns the device id of a stat result, or 0 when the platform does
// not expose one.
func deviceOf(info fs.FileInfo) uint64 {
	if st, ok := info.Sys().(*syscall.Stat_t); ok {
		return uint64(st.Dev)
	}
	return 0
}

func itoaU(n int) string { return strconv.Itoa(n) }
