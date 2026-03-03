# Boxyard shell integration — fuzzy box path widget
#
# Usage: source this file in your .zshrc:
#   source /path/to/boxyard/shell/boxyard.zsh
#
# Keybinding: Ctrl+G (customizable via BOXYARD_WIDGET_KEY)
#   export BOXYARD_WIDGET_KEY='^G'
#
# Type a partial box name, press Ctrl+G, and it will be replaced with the
# relative path to the matching box. If multiple boxes match, fzf is used
# to pick one.

_boxyard_box_widget() {
    local word="${LBUFFER##* }"

    local output
    output="$(boxyard-shell-helper search "$word" 2>/dev/null)"
    [[ -z "$output" ]] && return

    local line_count
    line_count="$(echo "$output" | wc -l | tr -d ' ')"

    local selected_path
    if [[ "$line_count" -eq 1 ]]; then
        selected_path="$(echo "$output" | cut -f2)"
    else
        local picked
        picked="$(echo "$output" | fzf --delimiter=$'\t' --with-nth=1 --select-1 --exit-0)"
        [[ -z "$picked" ]] && { zle redisplay; return; }
        selected_path="$(echo "$picked" | cut -f2)"
    fi

    [[ -z "$selected_path" ]] && { zle redisplay; return; }

    # Replace the current word in LBUFFER with the selected path
    LBUFFER="${LBUFFER%${word}}${selected_path}"
    zle redisplay
}

zle -N _boxyard_box_widget
bindkey "${BOXYARD_WIDGET_KEY:-^G}" _boxyard_box_widget
