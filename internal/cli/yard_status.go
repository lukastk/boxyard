package cli

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/runner"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/spf13/cobra"
)

func newYardStatusCommand() *cobra.Command {
	var (
		storageLocations []string
		outputFormat     string
		maxConcurrent    int
	)

	cmd := &cobra.Command{
		Use:   "yard-status",
		Short: "Get the sync status of all boxes in the yard",
		RunE: func(cmd *cobra.Command, args []string) error {
			if outputFormat != "text" && outputFormat != "json" {
				return &usageError{err: fmt.Errorf("invalid output format: %q", outputFormat)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			if len(storageLocations) == 0 {
				for name := range cfg.StorageLocations {
					storageLocations = append(storageLocations, name)
				}
			}
			for _, sl := range storageLocations {
				if _, ok := cfg.StorageLocations[sl]; !ok {
					fmt.Fprintf(os.Stderr, "Invalid storage location: [%s]\n", strings.Join(storageLocations, " "))
					os.Exit(1)
				}
			}
			if maxConcurrent == 0 {
				maxConcurrent = cfg.MaxConcurrentRcloneOps
			}

			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			var boxes []*models.BoxMeta
			for _, bm := range meta.BoxMetas {
				if containsString(storageLocations, bm.StorageLocation) {
					boxes = append(boxes, bm)
				}
			}

			client, err := rclone.New(cfg.RcloneConfigPath())
			if err != nil {
				return err
			}
			store := storage.New(client)
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()

			// Throttled, because this is one probe per box: on this fleet that
			// is 586 boxes, and letting them all go at once is what saturated
			// the storage box's connection limit before.
			tasks := make([]func(context.Context) (map[enums.BoxPart]statusPayload, error), len(boxes))
			for i := range boxes {
				bm := boxes[i]
				tasks[i] = func(ctx context.Context) (map[enums.BoxPart]statusPayload, error) {
					statuses, err := cmds.BoxSyncStatus(ctx, cfg, store, bm.IndexName())
					if err != nil {
						return nil, err
					}
					out := make(map[enums.BoxPart]statusPayload, len(statuses))
					for part, st := range statuses {
						out[part] = payloadOf(st)
					}
					return out, nil
				}
			}
			// No timeout: rclone's own per-call timeout applies inside, and a
			// wall-clock cap over the whole yard would kill a slow-but-working
			// pass on a large yard.
			results, err := runner.Throttle(ctx, maxConcurrent, 0, tasks)
			if err != nil {
				return err
			}

			// Grouped by storage location, each in REGISTRY order — the order
			// Python's dict is built in, and what its output is read in.
			slOrder := []string{}
			byLocation := map[string][]string{}
			payloads := map[string]map[enums.BoxPart]statusPayload{}
			for i, bm := range boxes {
				sl := bm.StorageLocation
				if _, seen := byLocation[sl]; !seen {
					slOrder = append(slOrder, sl)
				}
				byLocation[sl] = append(byLocation[sl], bm.IndexName())
				payloads[bm.IndexName()] = results[i]
			}

			if outputFormat == "json" {
				fmt.Println(renderYardStatusJSON(slOrder, byLocation, payloads))
				return nil
			}
			for _, sl := range slOrder {
				fmt.Println(sl + ":")
				for _, indexName := range byLocation[sl] {
					fmt.Println("    " + indexName + ":")
					for _, part := range enums.AllBoxParts {
						fmt.Println("        " + string(part) + ":")
						for _, line := range statusTextLines(payloads[indexName][part], 3) {
							fmt.Println(line)
						}
					}
				}
				// Python ends each storage location with `typer.echo("\n")`,
				// which is a blank line plus echo's own newline — two blank
				// lines. Reproduced rather than tidied: the separator is what
				// the output looks like, and a caller counting lines would
				// notice.
				fmt.Println()
				fmt.Println()
			}
			return nil
		},
	}

	f := cmd.Flags()
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil,
		"The storage location to get the status of. If not provided, the status of all storage locations will be shown.")
	f.StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	f.IntVarP(&maxConcurrent, "max-concurrent", "m", 0,
		"The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.")
	return cmd
}

// renderYardStatusJSON assembles the nested object by hand: encoding/json sorts
// map keys, and this output is grouped in registry order.
func renderYardStatusJSON(slOrder []string, byLocation map[string][]string,
	payloads map[string]map[enums.BoxPart]statusPayload) string {

	var b strings.Builder
	b.WriteString("{\n")
	for i, sl := range slOrder {
		fmt.Fprintf(&b, "  %q: {\n", sl)
		names := byLocation[sl]
		for j, indexName := range names {
			fmt.Fprintf(&b, "    %q: {\n", indexName)
			for k, part := range enums.AllBoxParts {
				body, err := marshalIndentedAt(payloads[indexName][part], 3)
				if err != nil {
					return "{}"
				}
				fmt.Fprintf(&b, "      %q: %s", string(part), body)
				if k < len(enums.AllBoxParts)-1 {
					b.WriteString(",")
				}
				b.WriteString("\n")
			}
			b.WriteString("    }")
			if j < len(names)-1 {
				b.WriteString(",")
			}
			b.WriteString("\n")
		}
		b.WriteString("  }")
		if i < len(slOrder)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("}")
	return b.String()
}
