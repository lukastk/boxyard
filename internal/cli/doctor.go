package cli

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/lukastk/boxyard/internal/doctor"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/spf13/cobra"
)

// maxListedFindings caps the per-finding list in the text output. The full list
// is always in `-o json`, which the hint says.
const maxListedFindings = 10

func newDoctorCommand() *cobra.Command {
	var (
		noRemote         bool
		storageLocations []string
		outputFormat     string
	)

	cmd := &cobra.Command{
		Use:   "doctor",
		Short: "Run a strictly read-only health check of this machine's boxyard state",
		RunE: func(cmd *cobra.Command, args []string) error {
			if outputFormat != "text" && outputFormat != "json" {
				return &usageError{err: fmt.Errorf("invalid output format: %q", outputFormat)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			for _, sl := range storageLocations {
				if _, ok := cfg.StorageLocations[sl]; !ok {
					fmt.Fprintf(os.Stderr, "Invalid storage location: [%s]\n", strings.Join(storageLocations, " "))
					os.Exit(1)
				}
			}

			// A missing rclone binary is a FINDING, not a failure to run: an
			// offline machine still wants the local checks.
			var store doctor.RemoteStore
			if client, err := rclone.New(cfg.RcloneConfigPath()); err == nil {
				store = storage.New(client)
			}

			doctor.Version = boxyardVersion
			report, err := doctor.Run(context.Background(), cfg, store, doctor.Options{
				CheckRemote:      !noRemote,
				StorageLocations: storageLocations,
			})
			if err != nil {
				return err
			}

			if outputFormat == "json" {
				fmt.Println(renderDoctorJSON(report))
			} else {
				printDoctorText(report)
			}
			if !report.Healthy {
				os.Exit(1)
			}
			return nil
		},
	}

	f := cmd.Flags()
	f.BoolVar(&noRemote, "no-remote", false,
		"Skip checks that access remote storage (stale-meta-mirror, tombstoned-box and diverged-box), so doctor works offline.")
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil,
		"Restrict the remote checks to the given storage location(s). Local checks always cover all storage locations.")
	f.StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	return cmd
}

func printDoctorText(report *doctor.Report) {
	for _, name := range doctor.CheckNames {
		check := report.Checks[name]
		if check.Skipped {
			fmt.Printf("- %s: skipped\n", name)
			continue
		}
		if len(check.Findings) == 0 {
			fmt.Printf("✓ %s: ok\n", name)
			continue
		}
		fmt.Printf("✗ %s: %d finding(s)\n", name, len(check.Findings))
		for _, finding := range check.Findings {
			fmt.Printf("    - %s\n", finding.Message)
			if missing := findingStrings(finding, "missing_index_names"); len(missing) > 0 {
				for i, indexName := range missing {
					if i >= maxListedFindings {
						break
					}
					fmt.Printf("        %s\n", indexName)
				}
				if len(missing) > maxListedFindings {
					fmt.Printf("        ... and %d more (use `-o json` for the full list)\n",
						len(missing)-maxListedFindings)
				}
			}
			fmt.Printf("      hint: %s\n", finding.Hint)
		}
	}
	fmt.Println("")
	if report.Healthy {
		fmt.Println("All checks passed.")
	} else {
		fmt.Printf("%d finding(s). See hints above.\n", report.NumFindings)
	}
}

func findingStrings(f doctor.Finding, key string) []string {
	for _, field := range f.Extra {
		if field.Key != key {
			continue
		}
		if xs, ok := field.Value.([]string); ok {
			return xs
		}
	}
	return nil
}

// renderDoctorJSON assembles the report by hand for two reasons: the checks are
// keyed by name and must come out in CHECK ORDER, which encoding/json's map
// handling would replace with alphabetical; and every string has to be escaped
// the way json.dumps does, with NON-ASCII as \uXXXX. That second one is not
// cosmetic here — one of this fleet's hostnames is "Lukas’s MacBook Pro", so
// the difference shows up in real findings.
func renderDoctorJSON(report *doctor.Report) string {
	var b strings.Builder
	b.WriteString("{\n")
	fmt.Fprintf(&b, "  \"healthy\": %t,\n", report.Healthy)
	fmt.Fprintf(&b, "  \"num_findings\": %d,\n", report.NumFindings)
	b.WriteString("  \"checks\": {\n")
	for i, name := range doctor.CheckNames {
		check := report.Checks[name]
		fmt.Fprintf(&b, "    %q: {\n", name)
		fmt.Fprintf(&b, "      \"skipped\": %t,\n", check.Skipped)
		if len(check.Findings) == 0 {
			b.WriteString("      \"findings\": []\n")
		} else {
			b.WriteString("      \"findings\": [\n")
			for j, finding := range check.Findings {
				b.WriteString("        {\n")
				fields := append([]doctor.Field{
					{Key: "message", Value: finding.Message},
					{Key: "hint", Value: finding.Hint},
				}, finding.Extra...)
				for k, field := range fields {
					value, err := strict.MarshalJSONIndentAt(field.Value, "          ")
					if err != nil {
						value = []byte(`""`)
					}
					fmt.Fprintf(&b, "          %q: %s", field.Key, value)
					if k < len(fields)-1 {
						b.WriteString(",")
					}
					b.WriteString("\n")
				}
				b.WriteString("        }")
				if j < len(check.Findings)-1 {
					b.WriteString(",")
				}
				b.WriteString("\n")
			}
			b.WriteString("      ]\n")
		}
		b.WriteString("    }")
		if i < len(doctor.CheckNames)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("  }\n}")
	return b.String()
}
