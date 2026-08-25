package cmds

import (
	"context"
	"fmt"
	"io"
	"path"

	"github.com/pelletier/go-toml/v2"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// ClaimBoxOptions mirrors the Python `claim_box` signature.
type ClaimBoxOptions struct {
	BoxIndexName string
	// Steal takes the box from another machine that currently owns it. Without
	// it, an already-owned box is refused.
	Steal   bool
	Verbose bool
	Out     io.Writer
}

// ClaimBox makes this machine the write owner of a box.
//
// Ported from pts/mod/cmds/15_claim_box.pct.py.
func ClaimBox(ctx context.Context, cfg *config.Config, s SyncStore, p syncengine.Perms, opts ClaimBoxOptions) (string, error) {
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	machineName, err := ownership.RequireMachineName(cfg, fmt.Sprintf("claim '%s'", opts.BoxIndexName))
	if err != nil {
		return "", err
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return "", fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}
	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	if slConfig.StorageType == config.StorageLocal {
		return "", &ownership.RefusedError{Message: fmt.Sprintf(
			"Cannot claim '%s': it is in the local storage location '%s', which no other "+
				"machine can reach, so there is nothing to coordinate.",
			opts.BoxIndexName, bm.StorageLocation)}
	}

	// Refuse a box this machine does not hold. The message names the exact
	// command that fixes it, because a refusal that does not is a refusal
	// people work around.
	if !bm.CheckIncluded(cfg) {
		return "", &ownership.RefusedError{Message: fmt.Sprintf(
			"Cannot claim '%s': it is not included on this machine, so this machine has no "+
				"DATA to push and claiming it would lock out every machine that does.\n"+
				"Include it first: `boxyard include -r '%s'`, then claim it.",
			opts.BoxIndexName, opts.BoxIndexName)}
	}

	// Bring META up to date before deciding anything — see ReleaseBox for why
	// a stale boxmeta makes this fail as a CONFLICT for reasons unrelated to
	// ownership.
	if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		Choices:      []enums.BoxPart{enums.PartMeta},
		Setting:      enums.SyncCareful,
	}); err != nil {
		return "", err
	}

	// Re-read from disk rather than trusting the cache: boxyard_meta.json is a
	// snapshot of the last refresh, and a META pull since then is exactly how
	// another machine's claim arrives here.
	boxMeta, err := models.LoadBoxMeta(cfg, bm.StorageLocation, opts.BoxIndexName)
	if err != nil {
		return "", err
	}
	previousOwner := boxMeta.WriteOwner
	if previousOwner == machineName {
		printf("'%s' is already owned by this machine (%s).\n", opts.BoxIndexName, machineName)
		return machineName, nil
	}
	if previousOwner != "" && !opts.Steal {
		return "", &ownership.RefusedError{Message: fmt.Sprintf(
			"Cannot claim '%s': it is owned by '%s'.\n"+
				"The tidy handover is `boxyard release -r '%s'` on '%s', then "+
				"`boxyard claim -r '%s'` here. If that machine is gone or unreachable, take it "+
				"over with `boxyard claim --steal -r '%s'`.",
			opts.BoxIndexName, previousOwner, opts.BoxIndexName, previousOwner,
			opts.BoxIndexName, opts.BoxIndexName)}
	}

	boxMeta.WriteOwner = machineName
	if err := boxMeta.Save(cfg); err != nil {
		return "", err
	}
	if _, err := SyncBox(ctx, cfg, s, p, SyncBoxOptions{
		BoxIndexName: opts.BoxIndexName,
		Choices:      []enums.BoxPart{enums.PartMeta},
		Setting:      enums.SyncCareful,
	}); err != nil {
		return "", err
	}

	// The read-back is the whole reason this is a command and not a boxmeta
	// edit. Two machines claiming at the same instant is last-write-wins, so
	// the only way to know whether this claim actually holds is to ask the
	// remote.
	remoteOwner, exists, err := remoteWriteOwner(ctx, cfg, s, boxMeta, opts.BoxIndexName)
	if err != nil {
		return "", err
	}
	if !exists {
		return "", &ownership.RefusedError{Message: fmt.Sprintf(
			"Claimed '%s' locally, but could not read the boxmeta back from '%s' to confirm "+
				"it. Do not rely on this claim; re-run `boxyard claim -r '%s'` once the remote "+
				"is reachable.",
			opts.BoxIndexName, boxMeta.StorageLocation, opts.BoxIndexName)}
	}
	if remoteOwner != machineName {
		return "", &ownership.RefusedError{Message: fmt.Sprintf(
			"Claim of '%s' did not stick: the remote boxmeta now names '%s', not '%s'. "+
				"Another machine claimed it at the same moment and won — claiming converges on "+
				"one owner but is not atomic.\n"+
				"This machine's local boxmeta will revert on the next sync. If you want the box "+
				"anyway, run `boxyard claim --steal -r '%s'`.",
			opts.BoxIndexName, remoteOwner, machineName, opts.BoxIndexName)}
	}

	if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
		return "", err
	}
	if previousOwner == "" {
		printf("Claimed '%s' for '%s'.\n", opts.BoxIndexName, machineName)
	} else {
		printf("Took '%s' from '%s'; it is now owned by '%s'.\n"+
			"Any unpushed work for this box on '%s' will be refused until it is discarded "+
			"or the box is released back.\n",
			opts.BoxIndexName, previousOwner, machineName, previousOwner)
	}
	return machineName, nil
}

// remoteWriteOwner reads the write_owner recorded on the remote.
//
// Decoded loosely on purpose: this asks one question, and a boxmeta carrying a
// key this build does not know must not make the answer an error.
func remoteWriteOwner(ctx context.Context, cfg *config.Config, s SyncStore,
	boxMeta *models.BoxMeta, boxIndexName string) (owner string, exists bool, err error) {

	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, boxMeta.StorageLocation, boxMeta.BoxID())
	if err != nil {
		return "", false, err
	}
	if remoteIndexName == "" {
		remoteIndexName = boxIndexName
	}
	slConfig, ok := cfg.StorageLocations[boxMeta.StorageLocation]
	if !ok {
		return "", false, fmt.Errorf("storage location '%s' not found", boxMeta.StorageLocation)
	}
	remotePath := path.Join(slConfig.StorePath, boxconst.RemoteBoxesRelPath,
		remoteIndexName, boxconst.BoxMetafileRelPath)

	found, raw, err := s.Cat(ctx, boxMeta.StorageLocation, remotePath)
	if err != nil || !found {
		return "", false, err
	}
	var fields map[string]any
	if err := toml.Unmarshal([]byte(raw), &fields); err != nil {
		return "", true, fmt.Errorf("remote boxmeta for '%s' is not valid TOML: %w", boxIndexName, err)
	}
	if v, ok := fields["write_owner"].(string); ok {
		return v, true, nil
	}
	return "", true, nil
}
