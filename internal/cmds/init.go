// Package cmds holds the command implementations — the layer between the CLI
// and the domain packages.
package cmds

import (
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
)

// InitOptions mirrors the Python `init_boxyard` signature.
type InitOptions struct {
	// ConfigPath and DataPath default to the standard locations when empty.
	ConfigPath string
	DataPath   string
	// Out receives the progress messages Python prints under `verbose`. A nil
	// Out means silent.
	Out io.Writer
}

// Init creates everything a usable boxyard needs, and creates ONLY what is
// missing — it is safe to re-run, which is how it doubles as a repair.
//
// Order matters and follows Python's: the config file first (everything else is
// derived from it), then the paths it names.
func Init(opts InitOptions) (*config.Config, error) {
	configPath := opts.ConfigPath
	if configPath == "" {
		configPath = boxconst.DefaultConfigPath
	}
	dataPath := opts.DataPath
	if dataPath == "" {
		dataPath = boxconst.DefaultDataPath
	}

	expandedConfig, err := config.ExpandUser(configPath)
	if err != nil {
		return nil, err
	}

	printf := func(format string, a ...any) {
		if opts.Out != nil {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	if configPath != boxconst.DefaultConfigPath {
		printf("Using a non-default config path. Set %s to it so boxyard uses it.\n",
			boxconst.EnvBoxyardConfigPath)
	}

	// --- the config file ---
	if _, err := os.Stat(expandedConfig); os.IsNotExist(err) {
		printf("Creating config file at: %s\n", configPath)
		if err := os.MkdirAll(filepath.Dir(expandedConfig), 0o755); err != nil {
			return nil, err
		}
		body := config.RenderDefault(configPath, dataPath)
		if err := os.WriteFile(expandedConfig, []byte(body), 0o644); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}

	cfg, err := config.Load(expandedConfig)
	if err != nil {
		return nil, err
	}

	// --- the default exclude list ---
	if _, err := os.Stat(cfg.DefaultRcloneExcludePath()); os.IsNotExist(err) {
		if err := os.WriteFile(cfg.DefaultRcloneExcludePath(),
			[]byte(boxconst.DefaultRcloneExclude), 0o644); err != nil {
			return nil, err
		}
	}

	// --- the data directories ---
	for _, p := range []string{cfg.BoxyardDataPath, cfg.LocalStorePath()} {
		if _, err := os.Stat(p); os.IsNotExist(err) {
			printf("Creating folder: %s\n", p)
			if err := os.MkdirAll(p, 0o755); err != nil {
				return nil, err
			}
		}
	}

	// --- one link per LOCAL storage location ---
	//
	// A local storage location keeps its store outside local_store, so
	// local_store needs an entry pointing at it. Without this the location is
	// configured and unusable.
	//
	// Python compared `storage_type != StorageType.LOCAL.value` here, against a
	// plain Enum — always true, so this loop did nothing at all and said
	// nothing about it. Fixed in Python v0.5.4; do not reintroduce it by
	// comparing against anything but the value itself.
	for name, sl := range cfg.StorageLocations {
		if sl.StorageType != config.StorageLocal {
			continue
		}
		store, err := config.ExpandUser(sl.StorePath)
		if err != nil {
			return nil, err
		}
		if err := os.MkdirAll(store, 0o755); err != nil {
			return nil, err
		}
		link := filepath.Join(cfg.LocalStorePath(), name)
		// Lstat, not Stat: a link pointing at a missing target must still be
		// replaced, and Stat would report it as absent.
		if _, err := os.Lstat(link); err == nil {
			if err := os.Remove(link); err != nil {
				return nil, fmt.Errorf("cannot replace the existing %s: %w", link, err)
			}
		}
		if err := os.Symlink(store, link); err != nil {
			return nil, err
		}
	}

	// --- the rclone config ---
	if _, err := os.Stat(cfg.RcloneConfigPath()); os.IsNotExist(err) {
		printf("Creating rclone config file at: %s\n", cfg.RcloneConfigPath())
		if err := os.WriteFile(cfg.RcloneConfigPath(), []byte("\n"), 0o644); err != nil {
			return nil, err
		}
	}

	printf("Done!\n\nYou can modify the config at: %s\nAll boxyard data is stored in: %s\n",
		configPath, cfg.BoxyardDataPath)
	return cfg, nil
}

// notPorted is the loud refusal used wherever the Go implementation does not
// yet cover a Python behaviour.
//
// Failing loudly is deliberate. A command that silently skips a step it does
// not understand diverges from the Python without anyone noticing, which is
// precisely what the parity suite exists to catch — so the unported paths
// refuse rather than approximate.
func notPorted(what string) error {
	return fmt.Errorf("%s is not yet supported by the Go implementation; use the Python boxyard for it", what)
}
