#!/bin/sh
# Reset the enterprise demo to its committed state: remove packs authored in
# rehearsals, restore tracked files, and regenerate the diagram files. State
# (settings, conversations) is untouched. Run before each live demo.
set -eu
git checkout -- projects/enterprise-demo
git clean -fd projects/enterprise-demo
./scripts/render-diagram.sh
echo "demo reset: workspace restored, diagrams regenerated"
