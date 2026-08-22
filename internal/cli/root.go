// Package cli implements boxyard's command-line interface.
//
// Ported from src/boxyard/_cli/. The Python uses typer, whose options are
// declared by type annotation; the closest Go equivalent is cobra with
// explicitly registered flags.
//
// The CLI surface is a hard contract. It is driven by ~40 call sites across
// myrig's zsh functions, mysystem's TypeScript, and a sesh plugin — several of
// which consume stdout directly (`cd $(boxyard path ...)`,
// `boxyard which -j | jq -r '.box_id'`). Flag names, short flags, exit codes
// and stdout format must all match.
package cli

import (
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/spf13/cobra"
)

// state holds what every subcommand needs.
type state struct {
	configPath string
}

var appState = &state{}

// Config loads the configuration named by the resolved config path.
func (s *state) Config() (*config.Config, error) {
	return config.Load(s.configPath)
}

// NewRootCommand builds the `boxyard` command tree.
func NewRootCommand() *cobra.Command {
	var configPath string

	root := &cobra.Command{
		Use:   "boxyard",
		Short: "Manage and sync folders (\"boxes\") across local and remote storage",
		// Errors are printed by Execute, and usage on every error would bury
		// the message that matters.
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			// Config path precedence: --config, then BOXYARD_CONFIG_PATH, then
			// the default. The Python CLI ignored the environment variable
			// until v0.4.0; both implementations honour it now.
			switch {
			case configPath != "":
				appState.configPath = configPath
			case os.Getenv(boxconst.EnvBoxyardConfigPath) != "":
				appState.configPath = os.Getenv(boxconst.EnvBoxyardConfigPath)
			default:
				appState.configPath = boxconst.DefaultConfigPath
			}
			return nil
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			// Typer's invoke_without_command prints help when no subcommand is
			// given.
			return cmd.Help()
		},
	}

	root.PersistentFlags().StringVar(&configPath, "config", "",
		"The path to the config file. Will be '~/.config/boxyard/config.toml' if not provided.")

	// Commands are registered as they are ported. A command is added only when
	// it is complete — a half-implemented command would silently diverge from
	// the Python for the flags it does not yet handle, which is exactly what
	// the parity suite exists to prevent.
	root.AddCommand(
		newWhichCommand(),
	)
	return root
}

// Execute runs the CLI and returns the process exit code.
func Execute() int {
	if err := NewRootCommand().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		return 1
	}
	return 0
}
