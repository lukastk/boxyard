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
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/spf13/cobra"
)

// boxyardVersion is what `--version` prints. It is a ROLLOUT GATE, not a
// nicety: rolling a change across the fleet means checking
// `ssh-target <machine> boxyard --version` on each one, so it has to report
// what is actually installed rather than what a source tree says.
//
// Set at build time with -ldflags "-X github.com/lukastk/boxyard/internal/cli.boxyardVersion=X.Y.Z".
// The default is deliberately not a number: an unstamped binary saying "0.0.0"
// would look like a real version to that rollout check.
var boxyardVersion = "dev"

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
	var showVersion bool

	root := &cobra.Command{
		Use:   "boxyard",
		Short: "Manage and sync folders (\"boxes\") across local and remote storage",
		// Errors are printed by Execute, and usage on every error would bury
		// the message that matters.
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			// --version is EAGER in the Python: it prints and exits before any
			// subcommand runs, and before the config is even resolved.
			if showVersion {
				fmt.Println(boxyardVersion)
				os.Exit(0)
			}
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
			if showVersion {
				fmt.Println(boxyardVersion)
				return nil
			}
			// Typer's invoke_without_command prints help when no subcommand is
			// given.
			return cmd.Help()
		},
	}

	root.PersistentFlags().StringVar(&configPath, "config", "",
		"The path to the config file. Will be '~/.config/boxyard/config.toml' if not provided.")
	root.PersistentFlags().BoolVar(&showVersion, "version", false,
		"Print the installed boxyard version and exit.")

	// A usage error exits 2, as click's do. Anything that fails to PARSE is one
	// — an unknown flag, a missing value, a bad enum. Runtime failures stay at
	// 1. Shell callers branch on this (`cd $(boxyard path ...) || ...`), so the
	// distinction is part of the contract.
	root.SetFlagErrorFunc(func(_ *cobra.Command, err error) error {
		return &usageError{err: err}
	})

	// Cobra volunteers `completion` and `help` subcommands. Typer has neither,
	// and an extra command is as much a surface difference as an extra flag —
	// `boxyard help` would work here and fail there.
	root.CompletionOptions.DisableDefaultCmd = true
	root.SetHelpCommand(&cobra.Command{Use: "no-help", Hidden: true})

	// Commands are registered as they are ported. A command is added only when
	// it is complete — a half-implemented command would silently diverge from
	// the Python for the flags it does not yet handle, which is exactly what
	// the parity suite exists to prevent.
	root.AddCommand(
		newInitCommand(),
		newWhichCommand(),
		newListCommand(),
		newListGroupsCommand(),
		newNewCommand(),
		newBoxStatusCommand(),
		newSyncCommand(),
		newPathCommand(),
		newAddToGroupCommand(),
		newRemoveFromGroupCommand(),
		newAddParentCommand(),
		newRemoveParentCommand(),
		newCreateUserSymlinksCommand(),
		newIncludeCommand(),
		newExcludeCommand(),
		newSyncMissingMetaCommand(),
		newClaimCommand(),
		newReleaseCommand(),
		newOwnerCommand(),
		newDiscardLocalCommand(),
		newDeleteCommand(),
		newRenameCommand(),
		newSyncNameCommand(),
		newCopyCommand(),
		newForcePushCommand(),
		newTreeCommand(),
		newYardStatusCommand(),
		newMultiSyncCommand(),
		newDoctorCommand(),
	)
	return root
}

// KNOWN DEVIATION: `boxyard <cmd> -h` prints help and exits 0, where the Python
// exits 2 with "No such option: -h" — typer has no `-h`. Registering `--help`
// without a shorthand does not suppress cobra's, and the alternatives are worse
// than the difference: nothing can depend on `-h` FAILING, and every flag name,
// short flag and usage-error exit code that a caller could depend on does
// match. Recorded rather than papered over.

// usageError marks a failure to parse the command line, which exits 2 rather
// than 1 — the code click uses for the same class of mistake.
type usageError struct{ err error }

func (e *usageError) Error() string { return e.err.Error() }
func (e *usageError) Unwrap() error { return e.err }

// Execute runs the CLI and returns the process exit code.
func Execute() int {
	root := NewRootCommand()
	if err := root.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		var usage *usageError
		if errors.As(err, &usage) || strings.HasPrefix(err.Error(), "unknown command") {
			return 2
		}
		return 1
	}
	return 0
}
