#!/usr/bin/env bash

node_override() {
  local node_override
  github_prefix="github:input-output-hk/cardano-node"
  # If argument is provided:
  if [ -n "${1:-""}" ]; then
    node_override="$github_prefix/$1"
  # else use specified branch and/or revision:
  elif [ -n "${NODE_REV:-""}" ]; then
    node_override="$github_prefix/$NODE_REV"
  elif [ -n "${NODE_BRANCH:-""}" ]; then
    node_override="$github_prefix/$NODE_BRANCH"
  elif [ -n "${NODE_PATH:-""}" ]; then
    node_override="path:$NODE_PATH"
  else
    #otherwise update to latest from default branch.
    node_override=$github_prefix
  fi
  echo --override-input cardano-node $node_override --recreate-lock-file
}
