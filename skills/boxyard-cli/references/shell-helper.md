# Shell integration and helper examples

Read this reference only for the relevant task. The [skill core](../SKILL.md)
contains authorization rules and routes to other bundled topics; examples here
are not permission to run mutating commands. Check `boxyard <command> --help`
for the installed CLI. Versioned observations and counts are historical, not
a live inventory.

## Shell helper

The repo includes a zsh helper:

```bash
source /path/to/boxyard/shell/boxyard.zsh
```

This is an optional shell-integration setup example, not a bundled skill path.
Use the separately deployed helper or an explicitly located source checkout;
never derive `/path/to/boxyard` from this skill's install location. Direct
`boxyard-shell-helper` commands below use the installed executable.

Default keybinding: `Ctrl+G` (`BOXYARD_WIDGET_KEY` can override it). Type a partial box name, press the keybinding, and it replaces the current word with a relative path to the selected box. It uses `boxyard-shell-helper search` and `fzf` for multiple matches.

Direct helper examples:

```bash
boxyard-shell-helper search TERM
boxyard-shell-helper search TERM --group GROUP
boxyard-shell-helper search TERM --included
boxyard-shell-helper search TERM --excluded
```
