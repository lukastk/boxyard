// Package ownership implements single-writer box ownership: a box may be
// included on any number of machines, but exactly one machine at a time may
// PUSH its DATA.
//
// The whole model is three rules:
//
//  1. Unowned means unrestricted. A box with no write_owner behaves exactly as
//     it did before v0.5 — ownership is opt-in per box, and a box nobody
//     claimed is nobody's to refuse. Anything else would mass-assign state to
//     hundreds of boxes nobody chose.
//  2. Ownership is checked before, and independently of, the sync setting.
//     FORCE is a SYNC-SAFETY override ("I accept overwriting"); ownership is a
//     COORDINATION statement ("another machine is the writer"). Conflating them
//     means the muscle-memory force used to unstick a box silently steals
//     ownership and, worse, leaves the remote holding this machine's data while
//     boxmeta.toml still names another as owner — a lie in shared state, which
//     is strictly worse than a refusal. There is deliberately no
//     --ignore-ownership flag for the same reason.
//  3. The routine refusal is not an error. See syncengine's WRITE_DENIED
//     condition: only DELIBERATE commands (claim, force-push, rename --scope
//     remote, delete, exclude) return an error, because a person typed them and
//     is waiting for an answer. The 20-minute sync loop must not manufacture
//     ~72 identical unresolvable failures a day.
//
// It is not a lock and does not pretend to be. Two machines claiming at the
// same instant is last-write-wins — measured at 5 times in 6 — so claim
// verifies by reading the remote back, and CONFLICT detection stays
// load-bearing.
//
// Ported from pts/mod/_ownership.pct.py.
package ownership

import (
	"context"
	"fmt"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
)

// RefusedError is returned when a deliberate, user-initiated action is refused
// because this machine is not the box's write owner (or has no machine name to
// be one with).
//
// Only ever returned out of a command a person typed. The routine sync path
// uses the WRITE_DENIED condition instead — see this package's doc comment.
type RefusedError struct{ Message string }

func (e *RefusedError) Error() string { return e.Message }

// MayPush reports whether this machine is allowed to push a box's DATA/CONF.
//
// An unset machine_name is treated as "not the owner", never as "the owner": a
// machine that cannot say who it is must not be able to claim it is the writer.
// That is the safe direction, and it costs nothing while a box is unowned.
func MayPush(cfg *config.Config, bm *models.BoxMeta) bool {
	if bm.WriteOwner == "" {
		return true // unowned == unrestricted
	}
	return bm.WriteOwner == cfg.MachineName
}

// RequireMachineName returns this machine's name, or refuses with a message
// that names the fix.
//
// Ownership identifies a machine by a CONFIGURED name and never by its
// hostname: one machine in this fleet has reported both `lukas-pocket4` and
// `pocket4`, and macOS reports user-editable pretty names.
func RequireMachineName(cfg *config.Config, action string) (string, error) {
	if cfg.MachineName == "" {
		return "", &RefusedError{Message: fmt.Sprintf(
			"Cannot %s: this machine has no name, so it cannot be recorded as a box's write owner.\n"+
				"Set `machine_name` in '%s' to this machine's canonical short name "+
				"(e.g. 'macbook' or 'mymain'), then try again.",
			action, cfg.ConfigPath)}
	}
	return cfg.MachineName, nil
}

// WriteDeniedMessage is the one-paragraph explanation shared by sync, doctor
// and multi-sync.
func WriteDeniedMessage(bm *models.BoxMeta, partLabel string) string {
	if partLabel == "" {
		partLabel = "DATA"
	}
	return fmt.Sprintf(
		"'%s' is owned by '%s', so the %s of this copy is not pushed. It still pulls.",
		bm.IndexName(), bm.WriteOwner, partLabel)
}

// WriteDeniedHint names the two ways out, both safe to run exactly as written.
//
// Every hint names an exact command that is safe to run verbatim. That is a
// rule with a scar behind it: doctor's duplicate-box-id hint used to say
// "delete or re-create one of them", and delete purges the remote and writes a
// tombstone keyed by box id — so following it destroys BOTH boxes.
//
// There are exactly two ways out of WRITE_DENIED, and both are always named:
// take the box over, or throw the local changes away. A refusal with only one
// escape is a refusal people work around.
func WriteDeniedHint(cfg *config.Config, bm *models.BoxMeta) string {
	return fmt.Sprintf(
		"Either take the box over with `boxyard claim --steal -r '%s'`, or throw away "+
			"this machine's changes with `boxyard discard-local -r '%s'` (which keeps a "+
			"copy under '%s'). The tidy handover is `boxyard release` on '%s' followed by "+
			"`boxyard claim` here.",
		bm.IndexName(), bm.IndexName(), cfg.LocalSyncBackupsPath(), bm.WriteOwner)
}

// OwnerGate refuses a deliberate remote-mutating action on a box another
// machine owns.
//
// Used by the paths that bypass the sync helper entirely and would otherwise
// write to the remote with no ownership check at all: force-push, rename
// --scope remote|both (it renames the remote directory), and delete (it purges
// the remote and writes a tombstone).
func OwnerGate(cfg *config.Config, bm *models.BoxMeta, action string) error {
	if MayPush(cfg, bm) {
		return nil
	}
	return &RefusedError{Message: fmt.Sprintf("Cannot %s: %s\n%s",
		action, WriteDeniedMessage(bm, ""), WriteDeniedHint(cfg, bm))}
}

// Checker is the comparison PushWouldTransfer needs. It is rclone.Client's
// Check, named as an interface so the probe can be tested without a remote.
type Checker interface {
	Check(ctx context.Context, src, dst rclone.Location, o rclone.TransferOptions) (answered bool, differing []string, err error)
}

// PushWouldTransfer asks whether pushing localPath to remote:remotePath would
// actually change anything, using the box's real filters.
//
// Mandatory, not an optimisation. Measured: creating `.DS_Store`,
// `__pycache__/x.pyc` or `.venv/pyvenv.cfg` flips a box to needs_push even
// though all three are in the default exclude list and the resulting push
// transfers nothing — the status comes from a tree walk, not a filter-aware
// one. Without this probe every read-only machine would report "you have local
// changes" forever, for changes that do not exist, and ownership would be
// unusable.
//
// (The deeper fix is to make the modification check filter-aware, which would
// also remove today's yard-wide no-op pushes. That is a separate, riskier
// change — getting rclone's filter semantics subtly wrong means silently not
// syncing real work.)
//
// Returns true when the answer is yes AND when the question could not be
// answered at all. Both callers treat true as "refuse to push and report it",
// so an unreachable remote surfaces as a reported state rather than as a silent
// all-clear — the one thing this must never do is claim a box is clean because
// it failed to look.
func PushWouldTransfer(ctx context.Context, c Checker, localPath, remote, remotePath, includePath, excludePath, filtersPath string) (bool, error) {
	answered, differing, err := c.Check(ctx,
		rclone.Local(localPath),
		rclone.Remote(remote, remotePath),
		rclone.TransferOptions{
			IncludeFile: includePath,
			ExcludeFile: excludePath,
			FiltersFile: filtersPath,
		})
	if err != nil {
		return true, err
	}
	if !answered {
		return true, nil
	}
	return len(differing) > 0, nil
}
