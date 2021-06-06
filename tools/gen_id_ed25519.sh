#!/usr/bin/env bash
set -ex

ssh-keygen -t ed25519 -N "" -C "$1" -f "$1-id_ed25519"
