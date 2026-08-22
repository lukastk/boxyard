package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/spf13/cobra"
)

// whichInfo is the `-j` payload. Field order is the key order Python's
// json.dumps produces, and mysystem's TypeScript reads it via jq.
type whichInfo struct {
	Name            string   `json:"name"`
	BoxID           string   `json:"box_id"`
	IndexName       string   `json:"index_name"`
	StorageLocation string   `json:"storage_location"`
	Groups          []string `json:"groups"`
	LocalDataPath   string   `json:"local_data_path"`
	Included        bool     `json:"included"`
}

// pyBool renders a bool the way Python does.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

func newWhichCommand() *cobra.Command {
	var (
		path          string
		jsonOutput    bool
		indexNameOnly bool
	)

	cmd := &cobra.Command{
		Use:   "which",
		Short: "Identify which box a path belongs to",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}

			target := path
			if target == "" {
				if target, err = os.Getwd(); err != nil {
					return err
				}
			}

			indexName, err := models.IndexNameFromSubPath(cfg, target)
			if err != nil {
				return err
			}
			if indexName == "" {
				fmt.Fprintln(os.Stderr, "Not inside a boxyard box.")
				os.Exit(1)
			}

			if indexNameOnly {
				fmt.Println(indexName)
				return nil
			}

			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			bm, ok := meta.ByIndexName()[indexName]
			if !ok {
				fmt.Fprintf(os.Stderr, "Box directory found (%s) but no matching metadata.\n", indexName)
				os.Exit(1)
			}

			dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
			if err != nil {
				return err
			}
			groups := bm.Groups
			if groups == nil {
				groups = []string{}
			}
			info := whichInfo{
				Name:            bm.Name,
				BoxID:           bm.BoxID(),
				IndexName:       bm.IndexName(),
				StorageLocation: bm.StorageLocation,
				Groups:          groups,
				LocalDataPath:   dataPath,
				Included:        bm.CheckIncluded(cfg),
			}

			if jsonOutput {
				out, err := strict.MarshalJSONIndent(info)
				if err != nil {
					return err
				}
				fmt.Println(string(out))
				return nil
			}

			groupsText := "(none)"
			if len(info.Groups) > 0 {
				groupsText = strings.Join(info.Groups, ", ")
			}
			fmt.Printf("name: %s\n", info.Name)
			fmt.Printf("box_id: %s\n", info.BoxID)
			fmt.Printf("index_name: %s\n", info.IndexName)
			fmt.Printf("storage_location: %s\n", info.StorageLocation)
			fmt.Printf("groups: %s\n", groupsText)
			fmt.Printf("local_data_path: %s\n", info.LocalDataPath)
			// Python's f-string renders a bool as "True"/"False". Shell callers
			// may be matching on that exact text, so it is reproduced verbatim
			// rather than lower-cased.
			fmt.Printf("included: %s\n", pyBool(info.Included))
			return nil
		},
	}

	cmd.Flags().StringVarP(&path, "path", "p", "", "The path to check. Defaults to current working directory.")
	cmd.Flags().BoolVarP(&jsonOutput, "json", "j", false, "Output as JSON.")
	cmd.Flags().BoolVarP(&indexNameOnly, "index-name", "i", false, "Only print the index name.")
	return cmd
}
