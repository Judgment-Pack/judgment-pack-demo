#!/bin/sh
# Reset the enterprise demo to its committed state: remove packs authored in
# rehearsals and restore tracked files. State (settings, conversations) is
# untouched. Run before each live demo.
set -eu
git checkout -- projects/enterprise-demo
git clean -fd projects/enterprise-demo
echo "demo reset: workspace restored"
