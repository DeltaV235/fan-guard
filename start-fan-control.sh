#!/bin/sh
# Alpine LXC startup: no environment file and no host Python required.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
fail() { echo "fan-control: $*" >&2; exit 1; }
case "${1:-}" in ''|--check) ;; *) fail 'usage: start-fan-control.sh [--check]' ;; esac
fan_device=''
for candidate in /sys/devices/platform/nct6687.2592/hwmon/hwmon*; do
    [ -f "$candidate/name" ] || continue
    [ "$(cat "$candidate/name")" = nct6687 ] || continue
    [ -z "$fan_device" ] || fail 'more than one matching hwmon device'
    fan_device=$candidate
done
[ -n "$fan_device" ] || fail 'nct6687.2592 is missing'
mask=$(cat /sys/module/nct6687/parameters/guard_mask)
[ "$((mask))" -eq 13 ] || fail "PVE guard_mask must be 13 (0x0d); got $mask"
for attr in pwm1 pwm1_enable pwm3 pwm3_enable pwm4 pwm4_enable fan_control_watchdog fan_control_authorize fan_control_status; do
    mounted=/opt/fan-control/$attr
    [ -f "$mounted" ] || fail "missing PVE bind mount: $mounted"
    # The separate LXC sysfs mount may have a different st_dev; compare sysfs inode.
    [ "$(stat -Lc %i "$mounted")" = "$(stat -Lc %i "$fan_device/$attr")" ] || fail "stale/wrong PVE bind mount: $mounted; stop and start LXC 131"
    [ "$attr" = fan_control_status ] || [ -w "$mounted" ] || fail "not writable: $mounted; check PVE udev permissions"
done
status=$(cat /opt/fan-control/fan_control_status)
printf '%s\n' "$status" | grep -Eq '(^|[[:space:]])mask=(13|0xd|0x0d)([[:space:]]|$)' || fail 'driver status mask is not 13'
printf '%s\n' "$status" | grep -Eq '(^|[[:space:]])failed_mask=(0|0x0)([[:space:]]|$)' || fail 'driver reports failed fallback; manual inspection required'
echo "fan-control: verified $fan_device; protected channels 1,3,4"
[ "${1:-}" != --check ] || exit 0
output=$(mktemp ./compose.yaml.XXXXXX)
trap 'rm -f "$output"' EXIT HUP INT TERM
sed "s|@FAN_DEVICE@|$fan_device|g" compose.template.yaml > "$output"
docker compose --env-file /dev/null -f "$output" config --quiet
mv "$output" compose.yaml
# Recreate mounts after a new hwmon instance / LXC boot; preserve named volumes.
docker compose --env-file /dev/null -f compose.yaml up -d --no-deps --force-recreate coolercontrol fan-guard
