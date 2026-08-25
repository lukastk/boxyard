package parity

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
)

// Differentials for the two pure functions `new_box` is built on: the git-URL
// name extraction and the box-id generator's timestamp handling.
//
// Both were written by reading the Python, and reading is how every parity gap
// in this port got in. Neither touches a box — the drivers are pure.

const pyGitURLDriver = `
import json, sys
from boxyard.cmds._new_box import _extract_box_name_from_git_url
print(json.dumps([_extract_box_name_from_git_url(u) for u in sys.argv[1:]]))
`

func TestExtractBoxNameFromGitURLMatchesPython(t *testing.T) {
	py := pythonBin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	urls := []string{
		"git@github.com:lukastk/boxyard.git",
		"git@github.com:lukastk/boxyard",
		"git@github.com:lukastk/nested/group/boxyard.git",
		"git@gitlab.example.com:2222/user/repo.git",
		"https://github.com/lukastk/boxyard.git",
		"https://github.com/lukastk/boxyard",
		"http://github.com/lukastk/boxyard/",
		"https://gitlab.com/group/sub/project.git",
		"https://user:token@github.com/lukastk/boxyard.git",
		"ssh://git@github.com/lukastk/boxyard.git",
		"/srv/git/bare-repo.git",
		"../relative/repo",
		"bare-repo",
		"repo.git///",
		"trailing.git",
		"has.dots.in.name",
		"weird.gitx",
	}

	args := append([]string{"-c", pyGitURLDriver}, urls...)
	out, err := exec.Command(py, args...).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatalf("python driver failed: %v", err)
	}
	var want []string
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(want) != len(urls) {
		t.Fatalf("driver returned %d results for %d urls", len(want), len(urls))
	}
	for i, url := range urls {
		if got := cmds.ExtractBoxNameFromGitURL(url); got != want[i] {
			t.Errorf("%q: Go=%q Python=%q", url, got, want[i])
		}
	}
}

// pyBoxIDCapability reports whether the installed Python already carries the
// fixed-timestamp fix (v0.5.5). Checked explicitly rather than by catching any
// driver failure: a differential that skips for an unexamined reason is
// indistinguishable from one that passes.
const pyBoxIDCapability = `
import json
try:
    from boxyard._models import format_creation_timestamp  # noqa: F401
    print("yes")
except ImportError:
    print("no")
`

const pyBoxIDDriver = `
import json, sys
from boxyard._models import generate_unique_box_id, format_creation_timestamp
from boxyard.config import BoxTimestampFormat, Config, StorageConfig, StorageType
from datetime import datetime, timezone
import pathlib

root = pathlib.Path(sys.argv[1])
config = Config(
    config_path=root / "config.toml",
    default_storage_location="s",
    boxyard_data_path=root / "data",
    box_timestamp_format=BoxTimestampFormat.DATE_ONLY,
    user_boxes_path=root / "boxes",
    user_box_groups_path=root / "groups",
    storage_locations={
        "s": StorageConfig(storage_type=StorageType.LOCAL, store_path=root / "store")
    },
    box_groups={},
    virtual_box_groups={},
    default_box_groups=[],
    box_subid_character_set="ab",
    box_subid_length=1,
    max_concurrent_rclone_ops=1,
)
ts, subid = generate_unique_box_id(
    config, existing_ids={"20240102_a"}, creation_timestamp="20240102"
)
print(json.dumps({
    "timestamp": ts,
    "subid": subid,
    "date_only": format_creation_timestamp(
        config, datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    ),
}))
`

// The fixed-timestamp path is the one Python got wrong: it checked
// `<now>_<subid>` for collisions and then substituted the caller's timestamp,
// so the id written was never the id checked. Both sides now thread the
// timestamp into the check, and this asserts they agree.
func TestFixedTimestampBoxIDMatchesPython(t *testing.T) {
	py := pythonBin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}
	capOut, err := exec.Command(py, "-c", pyBoxIDCapability).Output()
	if err != nil {
		t.Fatalf("capability probe failed: %v", err)
	}
	if strings.TrimSpace(string(capOut)) != "yes" {
		t.Skip("the installed boxyard predates the fixed-timestamp fix (v0.5.5); " +
			"re-run once it is deployed")
	}

	root := t.TempDir()
	out, err := exec.Command(py, "-c", pyBoxIDDriver, root).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatalf("python driver failed: %v", err)
	}
	var want struct {
		Timestamp string `json:"timestamp"`
		Subid     string `json:"subid"`
		DateOnly  string `json:"date_only"`
	}
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}

	cfg := &config.Config{
		BoxSubidCharacterSet: "ab",
		BoxSubidLength:       1,
		BoxTimestampFormat:   config.TimestampDateOnly,
	}
	ts, subid, err := models.GenerateUniqueBoxID(cfg, map[string]bool{"20240102_a": true}, "20240102")
	if err != nil {
		t.Fatal(err)
	}
	if ts != want.Timestamp || subid != want.Subid {
		t.Errorf("Go=(%q,%q) Python=(%q,%q)", ts, subid, want.Timestamp, want.Subid)
	}

	stamp := time.Date(2024, 1, 2, 3, 4, 5, 0, time.UTC)
	got, err := models.FormatCreationTimestamp(cfg, stamp)
	if err != nil {
		t.Fatal(err)
	}
	if got != want.DateOnly {
		t.Errorf("FormatCreationTimestamp: Go=%q Python=%q", got, want.DateOnly)
	}
}
