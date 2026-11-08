#!/bin/sh
set -eu
ip route replace unreachable 10.205.0.0/16 metric 42760
exec /usr/lib/frr/docker-start
