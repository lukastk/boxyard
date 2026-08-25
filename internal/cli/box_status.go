package cli

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/spf13/cobra"
)

// statusPayload is one part's status as `box-status -o json` renders it. Field
// order is pydantic's, because the JSON is consumed by shell callers.
type statusPayload struct {
	SyncCondition    string             `json:"sync_condition"`
	LocalPathExists  bool               `json:"local_path_exists"`
	RemotePathExists bool               `json:"remote_path_exists"`
	LocalSyncRecord  *models.SyncRecord `json:"local_sync_record"`
	RemoteSyncRecord *models.SyncRecord `json:"remote_sync_record"`
	IsDir            bool               `json:"is_dir"`
	ErrorMessage     *string            `json:"error_message"`
}

func payloadOf(s syncengine.SyncStatus) statusPayload {
	p := statusPayload{
		SyncCondition:    string(s.Condition),
		LocalPathExists:  s.LocalPathExists,
		RemotePathExists: s.RemotePathExists,
		LocalSyncRecord:  s.LocalSyncRecord,
		RemoteSyncRecord: s.RemoteSyncRecord,
		IsDir:            s.IsDir,
	}
	if s.ErrorMessage != "" {
		msg := s.ErrorMessage
		p.ErrorMessage = &msg
	}
	return p
}

func newBoxStatusCommand() *cobra.Command {
	var (
		sel          boxSelectorFlags
		outputFormat string
		// maxConcurrent is accepted for flag parity and does nothing here, as
		// in the Python: get_box_sync_status probes three paths and never
		// consults the limit.
		maxConcurrent int
	)

	cmd := &cobra.Command{
		Use:   "box-status",
		Short: "Get the sync status of a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			if outputFormat != "text" && outputFormat != "json" {
				return &usageError{err: fmt.Errorf("invalid output format: %q (want text or json)", outputFormat)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			if _, ok := meta.ByIndexName()[indexName]; !ok {
				fmt.Printf("Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			client, err := rclone.New(cfg.RcloneConfigPath())
			if err != nil {
				return err
			}
			statuses, err := cmds.BoxSyncStatus(context.Background(), cfg, storage.New(client), indexName)
			if err != nil {
				return err
			}

			// Part order is DATA, META, CONF — the order Python's dict is built
			// in, and shell callers read the text form positionally.
			ordered := make([]string, 0, len(enums.AllBoxParts))
			payloads := map[string]statusPayload{}
			for _, part := range enums.AllBoxParts {
				ordered = append(ordered, string(part))
				payloads[string(part)] = payloadOf(statuses[part])
			}

			if outputFormat == "json" {
				out, err := marshalOrderedStatus(ordered, payloads)
				if err != nil {
					return err
				}
				fmt.Println(string(out))
				return nil
			}
			for _, name := range ordered {
				fmt.Println(name + ":")
				for _, line := range statusTextLines(payloads[name], 1) {
					fmt.Println(line)
				}
			}
			return nil
		},
	}

	sel.register(cmd, selectorSpec{Noun: "sync", WithBoxPath: true, MatchCaseShort: "c"})
	cmd.Flags().StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	cmd.Flags().IntVar(&maxConcurrent, "max-concurrent", 0,
		"The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.")
	return cmd
}

// marshalOrderedStatus renders the three parts in order with `json.dumps(...,
// indent=2)`'s layout. encoding/json sorts map keys, which would put conf
// first, so the object is assembled by hand.
func marshalOrderedStatus(order []string, payloads map[string]statusPayload) ([]byte, error) {
	var b strings.Builder
	b.WriteString("{\n")
	for i, name := range order {
		body, err := json.MarshalIndent(payloads[name], "  ", "  ")
		if err != nil {
			return nil, err
		}
		fmt.Fprintf(&b, "  %q: %s", name, body)
		if i < len(order)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("}")
	return []byte(b.String()), nil
}

// statusTextLines reproduces `_dict_to_hierarchical_text`: four spaces per
// level, and Python's repr for scalars (True/False/None).
func statusTextLines(p statusPayload, indent int) []string {
	pad := strings.Repeat(" ", 4*indent)
	inner := strings.Repeat(" ", 4*(indent+1))
	var lines []string
	add := func(k, v string) { lines = append(lines, pad+k+": "+v) }

	add("sync_condition", p.SyncCondition)
	add("local_path_exists", pyBool(p.LocalPathExists))
	add("remote_path_exists", pyBool(p.RemotePathExists))
	for _, rec := range []struct {
		name string
		val  *models.SyncRecord
	}{{"local_sync_record", p.LocalSyncRecord}, {"remote_sync_record", p.RemoteSyncRecord}} {
		if rec.val == nil {
			add(rec.name, "None")
			continue
		}
		lines = append(lines, pad+rec.name+":")
		lines = append(lines,
			inner+"ulid: "+rec.val.ULID,
			inner+"timestamp: "+rec.val.Timestamp,
			inner+"sync_complete: "+pyBool(rec.val.SyncComplete),
			inner+"syncer_hostname: "+rec.val.SyncerHostname,
		)
	}
	add("is_dir", pyBool(p.IsDir))
	if p.ErrorMessage == nil {
		add("error_message", "None")
	} else {
		add("error_message", *p.ErrorMessage)
	}
	return lines
}

// handleResolveError turns boxref's sentinels into the CLI's exit codes.
//
// Dismissing the picker exits 0: "I changed my mind" is not a failure, and a
// nonzero exit there would make `cd $(boxyard path)` print an error.
func handleResolveError(err error) error {
	switch {
	case errors.Is(err, boxref.ErrPickCancelled):
		os.Exit(0)
	case errors.Is(err, boxref.ErrNotFound):
		fmt.Fprintln(os.Stderr, "Box not found.")
		os.Exit(1)
	}
	return err
}
