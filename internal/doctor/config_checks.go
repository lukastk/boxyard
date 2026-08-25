package doctor

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/rclone"
)

// Version is the running boxyard's version, used in the "upgrade this machine"
// hints. Set by the CLI; empty means say nothing about the version rather than
// invent one.
var Version string

func runningSuffix() string {
	if Version == "" {
		return ""
	}
	return " (running " + Version + ")"
}

// rcloneAvailable is recorded during the config check and consulted by the
// remote ones: with no rclone binary there is no point asking a remote
// anything.
type rcloneState struct{ available bool }

var rcloneOK = &rcloneState{available: true}

func checkRcloneConfig(cfg *config.Config, report *Report) {
	rcloneOK.available = true
	if _, err := rclone.Binary(); err != nil {
		rcloneOK.available = false
		report.add("rclone-config",
			fmt.Sprintf("The rclone binary could not be resolved: %v", err),
			fmt.Sprintf("Install rclone, or point boxyard at it via the %s env var or the `rclone_path` config key.",
				boxconst.EnvBoxyardRclone))
	}

	var rcloneSLNames []string
	for _, name := range sortedKeys(cfg.StorageLocations) {
		if cfg.StorageLocations[name].StorageType == config.StorageRclone {
			rcloneSLNames = append(rcloneSLNames, name)
		}
	}

	if len(rcloneSLNames) > 0 {
		confPath := cfg.RcloneConfigPath()
		raw, err := os.ReadFile(confPath)
		if err != nil {
			report.add("rclone-config",
				fmt.Sprintf("rclone config '%s' does not exist, but there are rclone storage locations: %s",
					confPath, strings.Join(rcloneSLNames, ", ")),
				"Boxyard uses its own rclone config (not ~/.config/rclone); recreate it with a remote per rclone storage location.",
				Field{"path", confPath})
		} else {
			sections := iniSections(string(raw))
			for _, name := range rcloneSLNames {
				if !sections[name] {
					report.add("rclone-config",
						fmt.Sprintf("Storage location '%s' has no [%s] remote in '%s'", name, name, confPath),
						fmt.Sprintf("Add a [%s] remote to the rclone config, or remove the storage location from the boxyard config.", name),
						Field{"storage_location", name})
				}
			}
		}
	}

	if _, err := os.Stat(cfg.DefaultRcloneExcludePath()); err != nil {
		report.add("rclone-config",
			fmt.Sprintf("Default exclude file '%s' does not exist", cfg.DefaultRcloneExcludePath()),
			"Data syncs of boxes without their own conf/.rclone_exclude will fail; re-run `boxyard init` to recreate it (existing config is preserved).",
			Field{"path", cfg.DefaultRcloneExcludePath()})
	}
}

// iniSections collects `[name]` headers. rclone's config is INI, and the only
// question asked of it here is which remotes exist.
func iniSections(text string) map[string]bool {
	out := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			out[strings.TrimSpace(line[1:len(line)-1])] = true
		}
	}
	return out
}

func checkUnknownBoxMetaKeys(cfg *config.Config, report *Report, sc *scan) {
	for _, bm := range sc.boxMetas {
		if len(bm.UnknownKeys) == 0 {
			continue
		}
		keys := make([]string, 0, len(bm.UnknownKeys))
		for k := range bm.UnknownKeys {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		report.add("unknown-boxmeta-keys",
			fmt.Sprintf("Box '%s' has boxmeta key(s) this boxyard does not know: %s",
				bm.IndexName(), strings.Join(keys, ", ")),
			fmt.Sprintf("The box was written by a newer boxyard. The key(s) are preserved "+
				"untouched, so nothing is lost and there is nothing to repair — but this "+
				"machine cannot act on what they mean. Upgrade boxyard here%s to the version that writes them.",
				runningSuffix()),
			Field{"index_name", bm.IndexName()},
			Field{"storage_location", bm.StorageLocation},
			Field{"unknown_keys", keys})
	}
}

func checkMachineName(cfg *config.Config, report *Report) {
	if cfg.MachineName != "" {
		return
	}
	report.add("machine-name-unset",
		fmt.Sprintf("No `machine_name` is configured in '%s'", cfg.ConfigPath),
		fmt.Sprintf("Nothing is broken by this today — box write-ownership is not yet "+
			"enforced — but this machine cannot own a box until it has a name. Set "+
			"`machine_name` to this machine's canonical short name (the same one "+
			"myrig uses, e.g. 'macbook' or 'mymain') in '%s', or export %s for a one-off.",
			cfg.ConfigPath, boxconst.EnvBoxyardMachineName),
		Field{"config_path", cfg.ConfigPath})
}

func checkUnknownConfigKeys(cfg *config.Config, report *Report) {
	if len(cfg.UnknownKeys) == 0 {
		return
	}
	keys := make([]string, 0, len(cfg.UnknownKeys))
	for k := range cfg.UnknownKeys {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	report.add("unknown-config-keys",
		fmt.Sprintf("Config '%s' has key(s) this boxyard does not know: %s",
			cfg.ConfigPath, strings.Join(keys, ", ")),
		fmt.Sprintf("They are ignored, not fatal. Either the config was written for a newer "+
			"boxyard -- upgrade this machine%s -- or the key is a typo, "+
			"in which case whatever it was meant to configure is silently not in "+
			"effect. Check the spelling against `boxyard init`'s generated config "+
			"before assuming the former.", runningSuffix()),
		Field{"config_path", cfg.ConfigPath},
		Field{"unknown_keys", keys})
}
