hermes_home := env("HERMES_HOME", home_directory() + "/.hermes")
plugin_dir := hermes_home + "/plugins/hermes-vikunja"

default:
    @just --list

# Symlink the plugin into a Hermes profile and show what it needs.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "$(dirname "{{plugin_dir}}")"
    ln -sfn "$PWD" "{{plugin_dir}}"
    echo "linked {{plugin_dir}} -> $PWD"
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
