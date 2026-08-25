package cli

import (
	"os"

	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/spf13/cobra"
)

func newInitCommand() *cobra.Command {
	var configPath, dataPath string

	cmd := &cobra.Command{
		Use:   "init",
		Short: "Initialise boxyard's config and data directories",
		RunE: func(cmd *cobra.Command, args []string) error {
			// `init` has its own --config-path, which predates the global
			// --config. It takes precedence, but when absent this falls back to
			// the SAME resolution every other command uses (global --config,
			// then BOXYARD_CONFIG_PATH, then the default) rather than jumping
			// straight to the default — otherwise
			// `boxyard --config <sandbox> init` silently initialises the real
			// config instead of the one named.
			if configPath == "" {
				configPath = appState.configPath
			}
			_, err := cmds.Init(cmds.InitOptions{
				ConfigPath: configPath,
				DataPath:   dataPath,
				Out:        os.Stdout,
			})
			return err
		},
	}

	cmd.Flags().StringVar(&configPath, "config-path", "",
		"The path to the config file. Will be ~/.config/boxyard/config.toml if not provided.")
	cmd.Flags().StringVar(&dataPath, "data-path", "",
		"The path to the data directory. Will be ~/.boxyard if not provided.")
	return cmd
}
