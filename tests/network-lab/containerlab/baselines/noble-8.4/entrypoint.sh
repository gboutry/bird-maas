#!/bin/sh
set -eu
# Docker management must never carry an unresolved lab data destination.
ip route replace unreachable 10.205.0.0/16 metric 42760
/usr/lib/frr/frrinit.sh start
trap '/usr/lib/frr/frrinit.sh stop; exit 0' TERM INT
while :; do
    sleep 3600 &
    wait "$!" || true
done
