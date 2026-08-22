// Command boxyard manages and syncs folders ("boxes") across local and remote
// storage using rclone.
package main

import (
	"os"

	"github.com/lukastk/boxyard/internal/cli"
)

func main() {
	os.Exit(cli.Execute())
}
