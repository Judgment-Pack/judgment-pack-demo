#!/bin/sh
# Reset the enterprise demo to its committed state: remove packs authored in
# rehearsals and restore tracked files. State (settings, conversations) is
# untouched. Run before each live demo.
set -eu
if [ -n "$(git status --short projects/enterprise-demo)" ]; then
  echo "discarding these uncommitted changes under projects/enterprise-demo:"
  git status --short projects/enterprise-demo
fi
git checkout -- projects/enterprise-demo
git clean -fd projects/enterprise-demo
echo "demo reset: workspace restored"
