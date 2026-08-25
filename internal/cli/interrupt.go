package cli

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/lukastk/boxyard/internal/boxconst"
)

// softInterrupt returns a context that is cancelled on the FIRST interrupt, so
// a long sync stops at the next box-part boundary rather than mid-transfer.
//
// Ported from `enable_soft_interruption`. SIGINT, SIGTERM and SIGHUP are all
// caught — the last one because the supervisor's sync loop dies with the
// terminal, and a transfer killed mid-flight is what leaves an incomplete sync
// record behind.
//
// The count matters: after SoftInterruptCount signals the process exits at
// once. Someone hammering Ctrl-C wants out now, and refusing to die is how a
// tool earns a `kill -9` habit.
func softInterrupt(parent context.Context) (context.Context, func()) {
	ctx, cancel := context.WithCancel(parent)
	ch := make(chan os.Signal, boxconst.SoftInterruptCount+1)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)

	go func() {
		count := 0
		for sig := range ch {
			count++
			if count < boxconst.SoftInterruptCount {
				fmt.Fprintf(os.Stderr,
					"\nWARNING: %s received (%d/%d) — will stop after the current operation.\n",
					sig, count, boxconst.SoftInterruptCount)
				cancel()
				continue
			}
			fmt.Fprintf(os.Stderr, "\n%s received %d times — exiting immediately.\n",
				sig, boxconst.SoftInterruptCount)
			os.Exit(1)
		}
	}()

	return ctx, func() {
		signal.Stop(ch)
		close(ch)
		cancel()
	}
}
