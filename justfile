hermes_home := env("HERMES_HOME", home_directory() + "/.hermes")
plugin_dir := hermes_home + "/plugins/hermes-vikunja"
# The gateway host; the checkout lives here on that host and is what `install` links.
remote := env("HERMES_REMOTE", "kita@cat.chi1.cappuchino.xyz")
remote_dir := env("HERMES_REMOTE_DIR", "Projects/hermes-vikunja")

default:
    @just --list

# Symlink the plugin into a Hermes profile, and the board manual with it.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "$(dirname "{{plugin_dir}}")"
    ln -sfn "$PWD" "{{plugin_dir}}"
    echo "linked {{plugin_dir}} -> $PWD"
    # The board manual is versioned here and read from ~/.hermes, so the profile
    # keeps a symlink instead of a copy that drifts.
    manual="{{hermes_home}}/vikunja-a2a.md"
    if [ -f "$manual" ] && [ ! -L "$manual" ]; then
        backup="$manual.bak-$(date +%Y%m%dT%H%M%S)"
        cp "$manual" "$backup"
        echo "kept the unversioned manual as $backup"
    fi
    ln -sfn "$PWD/vikunja-a2a.md" "$manual"
    echo "linked $manual -> $PWD/vikunja-a2a.md"
    echo "next: just validate && hermes plugins enable hermes-vikunja"

# Compile check plus a look at the imports this plugin relies on.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    python3 -m py_compile adapter.py __init__.py
    echo "adapter.py and __init__.py compile"
    grep -n "^from gateway" adapter.py

# Run Hermes' own plugin validator against the installed directory.
validate:
    hermes plugins validate {{plugin_dir}}

# One-way mirror of this checkout onto the gateway host.
sync:
    rsync -a --delete --exclude .git --exclude __pycache__ ./ {{remote}}:{{remote_dir}}/

# Sync, then ask the gateway for a restart that waits for idle.
deploy: sync
    #!/usr/bin/env bash
    set -euo pipefail
    ssh {{remote}} '~/.local/bin/hermes gateway restart' || true
    echo "restart finished on {{remote}}"

# Push every save: sync always, restart the gateway when a file it loads changed.
# Polls instead of using inotify so it needs nothing installed and survives reboots.
watch:
    #!/usr/bin/env bash
    set -euo pipefail
    tree() {
        find . -path ./.git -prune -o -path ./__pycache__ -prune -o -type f -printf '%T@ %s %p\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1
    }
    loaded() { sha256sum adapter.py __init__.py plugin.yaml 2>/dev/null | sha256sum | cut -d' ' -f1; }
    echo "watching $PWD -> {{remote}}:{{remote_dir}} (ctrl-c to stop)"
    last_tree=$(tree)
    last_loaded=$(loaded)
    while sleep 2; do
        now=$(tree)
        [ "$now" = "$last_tree" ] && continue
        last_tree=$now
        rsync -a --delete --exclude .git --exclude __pycache__ ./ {{remote}}:{{remote_dir}}/
        echo "$(date +%H:%M:%S) synced"
        now_loaded=$(loaded)
        if [ "$now_loaded" != "$last_loaded" ]; then
            last_loaded=$now_loaded
            # The restart command waits for idle in the foreground, so run it
            # detached: the loop keeps syncing while the gateway drains.
            setsid ssh -o BatchMode=yes {{remote}} '~/.local/bin/hermes gateway restart' \
                >/dev/null 2>&1 < /dev/null &
            echo "$(date +%H:%M:%S) gateway restart requested (drains first)"
        fi
    done
