package cli

import (
	"os"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/spf13/cobra"
)

// boxSelectorFlags are the six flags every box-addressing command shares.
// Registering them from one place is what keeps their names, short flags and
// help text identical across ~15 commands.
type boxSelectorFlags struct {
	BoxPath      string
	BoxIndexName string
	BoxID        string
	BoxName      string
	MatchMode    string
	MatchCase    bool
}

// resolve turns the flags into an index name.
//
// --box-path wins outright when given, matching the Python, which resolves it
// before consulting anything else. With no selector at all the box the user is
// standing in wins; see boxref.Options.CurrentBoxIndexName.
func (f *boxSelectorFlags) resolve(cfg *config.Config, metas []*models.BoxMeta, opts boxref.Options) (string, error) {
	if f.BoxPath != "" {
		indexName, err := models.IndexNameFromSubPath(cfg, f.BoxPath)
		if err != nil {
			return "", err
		}
		if indexName != "" {
			return indexName, nil
		}
	}

	opts.BoxName = f.BoxName
	opts.BoxID = f.BoxID
	opts.BoxIndexName = f.BoxIndexName
	opts.MatchMode = boxref.MatchMode(f.MatchMode)
	opts.MatchCase = f.MatchCase

	if cwd, err := os.Getwd(); err == nil {
		// A failure here is not fatal: it only means no box can be inferred,
		// and the picker still covers that case.
		if indexName, err := models.IndexNameFromSubPath(cfg, cwd); err == nil {
			opts.CurrentBoxIndexName = indexName
		}
	}

	return boxref.Resolve(metas, boxref.FZF{}, opts)
}

// selectorSpec is the per-command variation in the shared selector flags. The
// differences are real and deliberate, not drift:
//
//   - `sync` has no `-c` on --name-match-case, because it spends `-c` on
//     --sync-choices;
//   - `path`, `include` and `exclude` have no --box-path at all;
//   - `path` words --box-name differently.
//
// They are captured here so a command cannot quietly acquire a flag the Python
// does not have, which is the divergence the parity suite exists to catch.
type selectorSpec struct {
	// Noun completes "The path to the box to <noun>." Empty omits that flag.
	Noun string
	// WithBoxPath registers --box-path/-p.
	WithBoxPath bool
	// MatchCaseShort is the short flag for --name-match-case, or "" for none.
	MatchCaseShort string
	// BoxNameHelp overrides the --box-name help text.
	BoxNameHelp string
}

// register adds the shared selector flags to a command.
func (f *boxSelectorFlags) register(cmd *cobra.Command, spec selectorSpec) {
	fs := cmd.Flags()
	if spec.WithBoxPath {
		fs.StringVarP(&f.BoxPath, "box-path", "p", "", "The path to the box to "+spec.Noun+".")
	}
	fs.StringVarP(&f.BoxIndexName, "box", "r", "", "The index name of the box, in the form '{ULID}__{BOX_NAME}'.")
	fs.StringVarP(&f.BoxID, "box-id", "i", "", "The id of the box to "+spec.Noun+".")
	boxNameHelp := spec.BoxNameHelp
	if boxNameHelp == "" {
		boxNameHelp = "The name of the box to " + spec.Noun + "."
	}
	fs.StringVarP(&f.BoxName, "box-name", "n", "", boxNameHelp)
	fs.StringVarP(&f.MatchMode, "name-match-mode", "m", "", "The mode to use for matching the box name.")
	fs.BoolVarP(&f.MatchCase, "name-match-case", spec.MatchCaseShort, false,
		"Whether to match the box name case-sensitively.")
}
