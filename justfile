hermes_home := env("HERMES_HOME", home_directory() + "/.hermes")

default:
    @just --list

# Symlink the plugin into a Hermes profile and show what it needs.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    target="{{hermes_home}}/plugins/hermes-vikunja"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$PWD" "$target"
    echo "linked $target -> $PWD"
    echo "next: hermes plugins validate hermes-vikunja && hermes plugins enable hermes-vikunja"

# Compile check plus a look at the imports this plugin relies on.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    python3 -m py_compile adapter.py
    echo "adapter.py compiles"
    grep -n "^from gateway" adapter.py

# Run Hermes' own plugin validator against this directory.
validate:
    hermes plugins validate hermes-vikunja
