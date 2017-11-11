#!/usr/bin/env bash
set -ex

ssh-keygen -b 4096 -t rsa -N "" -C "$1" -f "$1-id_rsa"
