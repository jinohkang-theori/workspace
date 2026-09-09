#!/bin/bash
set -e

# shellcheck source=/dev/null
source dev-container-features-test-lib

CONF=/usr/local/share/storage-tuning/config.env

check "helperImage baked in" grep -q 'STORAGE_TUNING_HELPER_IMAGE:=docker.io/library/busybox:stable' "$CONF"
check "warmReaders baked in" grep -q 'STORAGE_TUNING_WARM_READERS:=4' "$CONF"
check "timeoutSeconds baked in" grep -q 'STORAGE_TUNING_TIMEOUT:=0' "$CONF"
check "verbose baked in" grep -q 'STORAGE_TUNING_VERBOSE:=true' "$CONF"
# shellcheck disable=SC2016
check "environment overrides config" bash -c '
    out=$(STORAGE_TUNING_DOCKER_SOCKET=/nonexistent/override.sock storage-tuning --tune) \
    && echo "$out" | grep -q "/nonexistent/override.sock is not present"'

reportResults
