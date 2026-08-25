package config

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// pythonWithBoxyard finds an interpreter that can import boxyard. The system
// python3 usually cannot — boxyard is installed as a uv TOOL, in its own venv —
// and getting this wrong does not fail, it SKIPS, which looks identical to
// passing.
func pythonWithBoxyard() string {
	home, err := os.UserHomeDir()
	if err == nil {
		p := filepath.Join(home, ".local", "share", "uv", "tools", "boxyard", "bin", "python3")
		if exec.Command(p, "-c", "import boxyard").Run() == nil {
			return p
		}
	}
	if exec.Command("python3", "-c", "import boxyard").Run() == nil {
		return "python3"
	}
	return ""
}

const pyRenderDriver = `
import json, sys, tomli_w
from boxyard.config import _get_default_config_dict
cfg, data = sys.argv[1] or None, sys.argv[2] or None
d = _get_default_config_dict(config_path=cfg, data_path=data)
del d["config_path"]
print(json.dumps({"toml": tomli_w.dumps(d)}))
`

// TestRenderDefaultMatchesPython is a DIFFERENTIAL. config.toml is the one
// artefact both implementations must agree on before anything else runs, and
// during a migration a config written by one is read by the other.
func TestRenderDefaultMatchesPython(t *testing.T) {
	py := pythonWithBoxyard()
	if py == "" {
		t.Skip("python3 has no boxyard installed — nothing to compare against")
	}

	cases := []struct{ configPath, dataPath string }{
		{"", ""}, // the defaults, which is what `boxyard init` writes
		{"/tmp/bx/config.toml", "/tmp/bx/data"},
		{"~/elsewhere/config.toml", "~/elsewhere/data"},
		// A path with a quote and a backslash: generated, not user input, but
		// the escaping must still produce a file that parses.
		{`/tmp/we"ird/config.toml`, `/tmp/we"ird/data`},
	}

	for _, c := range cases {
		name := c.dataPath
		if name == "" {
			name = "(defaults)"
		}
		t.Run(name, func(t *testing.T) {
			out, err := exec.Command(py, "-c", pyRenderDriver, c.configPath, c.dataPath).Output()
			if err != nil {
				if ee, ok := err.(*exec.ExitError); ok {
					t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
				}
				t.Fatalf("python driver failed: %v", err)
			}
			var got struct {
				TOML string `json:"toml"`
			}
			if err := json.Unmarshal(out, &got); err != nil {
				t.Fatalf("driver output %q: %v", out, err)
			}
			if g := RenderDefault(c.configPath, c.dataPath); g != got.TOML {
				t.Errorf("byte mismatch with tomli_w\n  go:     %q\n  python: %q", g, got.TOML)
			}
		})
	}
}

// TestRenderDefaultRoundTrips — the rendered file must load back through this
// package's own parser, or `init` would write a config it cannot read.
func TestRenderDefaultRoundTrips(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(p, []byte(RenderDefault(p, filepath.Join(dir, "data"))), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(p)
	if err != nil {
		t.Fatalf("init would write a config it cannot read: %v", err)
	}
	if cfg.DefaultStorageLocation != "fake" {
		t.Errorf("default_storage_location = %q", cfg.DefaultStorageLocation)
	}
	sl, ok := cfg.StorageLocations["fake"]
	if !ok {
		t.Fatal("the default storage location is missing from the rendered config")
	}
	if sl.StorageType != StorageLocal {
		t.Errorf("storage_type = %q, want local", sl.StorageType)
	}
	if len(cfg.UnknownKeys) != 0 {
		t.Errorf("the rendered config carries unknown keys: %v", cfg.UnknownKeys)
	}
}
