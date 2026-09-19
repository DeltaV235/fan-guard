#!/usr/bin/env python3
"""Lease keeper for the opt-in nct6687 guard ABI. Python standard library only."""
import argparse
import copy
import errno
from http.client import HTTPException
import datetime
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import time
from urllib.error import HTTPError
import urllib.parse
import urllib.request


class Unsafe(RuntimeError):
    pass


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Driver:
    def __init__(self, path, mask):
        self.path, self.mask = Path(path), mask

    def write(self, name, value):
        with (self.path / name).open('w') as f:
            f.write(str(value) + '\n')

    def status(self):
        raw = dict(x.split('=', 1) for x in (self.path / 'fan_control_status').read_text().split())
        s = {k: v if k == 'state' else int(v) for k, v in raw.items()}
        if s['abi'] != 1 or s['mask'] != self.mask or s['state'] not in ('locked', 'active', 'failed'):
            raise Unsafe('driver identity/ABI/mask mismatch')
        for key in ('generation', 'remaining_ms', 'failed_mask', 'manual_mask'):
            if s[key] < 0:
                raise Unsafe('invalid driver status')
        return s

    def fallback(self):
        self.write('fan_control_watchdog', 0)

    def arm(self, generation, seconds):
        self.write('fan_control_authorize', f'{generation} {seconds}')

    def renew(self, seconds):
        self.write('fan_control_watchdog', seconds)


class API:
    def __init__(self, config, clock=time.monotonic):
        self.base = config['api_url'].rstrip('/')
        url = urllib.parse.urlsplit(self.base)
        if url.scheme not in ('http', 'https') or url.username or url.password:
            raise Unsafe('API URL must use HTTP(S), without embedded credentials')
        self.token_file = config.get('token_file')
        self.clock = clock
        self.deadline = 0
        # Never send the bearer token through inherited HTTP proxy settings.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def begin(self, budget=6):
        self.deadline = self.clock() + budget

    def request(self, method, path, body=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise Unsafe('API cycle deadline')
        headers = {'Content-Type': 'application/json'}
        # Runtime-only credential access: never log headers, body or raw HTTP errors.
        if self.token_file:
            headers['Authorization'] = 'Bearer ' + Path(self.token_file).read_text().strip()
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        with self.opener.open(request, timeout=min(2, remaining)) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or self.clock() >= self.deadline:
            raise Unsafe('API response exceeds budget')
        return json.loads(raw) if raw else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Unsafe('API redirects are forbidden')


def quoted(value):
    return urllib.parse.quote(value, safe='')


class Health:
    def __init__(self, config, api, clock=time.monotonic):
        self.cfg, self.api, self.clock = config, api, clock
        self.seen = {}
        self.observed = None
        self.plan = None
        self.expected_mask = 0

    def profile_sources(self, uid, profiles, stack=()):
        if uid in stack or uid not in profiles:
            raise Unsafe('missing or cyclic profile')
        p = profiles[uid]
        kind = p['p_type']
        if kind == 'Graph':
            src = p['temp_source']
            return {(src['device_uid'], src['temp_name'])}
        if kind in ('Fixed', 'Default'):
            return set()
        if kind in ('Mix', 'Overlay'):
            members = p['member_profile_uids']
            if not members:
                raise Unsafe('empty profile members')
            return set().union(*(self.profile_sources(x, profiles, stack + (uid,)) for x in members))
        raise Unsafe('unsupported current profile')

    @staticmethod
    def setting(value):
        keys = [k for k in ('profile_uid', 'speed_fixed', 'reset_to_default') if k in value]
        if len(keys) != 1:
            raise Unsafe('missing or ambiguous channel setting')
        key = keys[0]
        val = value[key]
        if key == 'profile_uid' and (not isinstance(val, str) or not val or len(val) > 256):
            raise Unsafe('invalid profile reference')
        if key == 'speed_fixed' and (type(val) is not int or not 0 <= val <= 100):
            raise Unsafe('invalid fixed duty')
        if key == 'reset_to_default' and val is not True:
            raise Unsafe('invalid reset setting')
        return {key: val}

    def validate_settings(self, snapshot):
        if not isinstance(snapshot, list) or len(snapshot) != len(self.cfg['channels']):
            raise Unsafe('invalid recovery snapshot')
        normalized = []
        for channel, entry in zip(self.cfg['channels'], snapshot):
            if any(entry[k] != channel[k] for k in ('device_uid', 'channel_name', 'pwm')):
                raise Unsafe('snapshot channel identity mismatch')
            normalized.append(dict(channel, setting=self.setting(entry['setting'])))
        return normalized

    def read_settings(self):
        devices = {}
        for uid in {c['device_uid'] for c in self.cfg['channels']}:
            settings = self.api.request('GET', '/devices/' + quoted(uid) + '/settings')['settings']
            devices[uid] = {s['channel_name']: s for s in settings}
        result = []
        for channel in self.cfg['channels']:
            setting = devices[channel['device_uid']].get(channel['channel_name'])
            if setting is None:
                # CoolerControl removes the saved channel entry on reset.
                # Actual device/channel presence is checked separately in /status.
                setting = {'reset_to_default': True}
            result.append(dict(channel, setting=self.setting(setting)))
        return result

    def snapshot(self, locked=False, recovery_settings=None):
        self.api.begin()
        observed = self.read_settings()
        plan = self.validate_settings(recovery_settings) if recovery_settings is not None else observed
        profiles = {p['uid']: p for p in self.api.request('GET', '/profiles')['profiles']}
        sources = {(s['device_uid'], s['temp_name']) for s in self.cfg['required_sources']}
        expected_mask = 0
        for entry in observed + plan:
            setting = entry['setting']
            if 'profile_uid' in setting:
                sources |= self.profile_sources(setting['profile_uid'], profiles)
        for entry in plan:
            setting = entry['setting']
            software = ('speed_fixed' in setting or
                        ('profile_uid' in setting and profiles[setting['profile_uid']]['p_type'] != 'Default'))
            if software:
                expected_mask |= 1 << (entry['pwm'] - 1)
        if self.read_settings() != observed:
            raise Unsafe('channel settings changed during health snapshot')
        # Explicit sources include physical inputs underlying custom sensors.
        # Fail closed on unrelated source health as well: safer than underestimating dependencies.
        health = self.api.request('GET', '/devices/health')
        for key in ('failsafe', 'unreachable', 'missing', 'stale_source', 'firmware_overrides'):
            if not isinstance(health[key], list):
                raise Unsafe('unsupported health schema')
        if any(health[k] for k in ('unreachable', 'missing', 'stale_source', 'firmware_overrides')):
            raise Unsafe('device/source health degraded')
        controlled = {(c['device_uid'], c['channel_name']) for c in self.cfg['channels']}
        output_healthy = True
        for item in health['failsafe']:
            # While locked, only the guarded output channels may have expected errors.
            # Temperature failures are never waived. Recovery must clear all channel failures.
            if not (locked and item['kind'] == 'Channel' and
                    (item['device_uid'], item['name']) in controlled):
                raise Unsafe('failsafe data')
            output_healthy = False
        devices = {d['uid']: d for d in self.api.request('GET', '/status')['devices']}
        now = self.clock()
        required = {uid for uid, _ in sources} | {uid for uid, _ in controlled}
        for uid in required:
            latest = devices[uid]['status_history'][-1]
            timestamp = datetime.datetime.fromisoformat(latest['timestamp'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise Unsafe('status timestamp must include timezone')
            previous = self.seen.get(uid)
            if previous is None or timestamp > previous[0]:
                self.seen[uid] = (timestamp, now)
            elif timestamp < previous[0]:
                self.seen.clear()
                raise Unsafe('timestamp moved backwards; reacquire stable history')
            if now - self.seen[uid][1] >= 6:
                raise Unsafe('status stopped advancing')
            temps = {t['name']: t['temp'] for t in latest['temps']}
            for source_uid, name in sources:
                if source_uid == uid and not number(temps.get(name)):
                    raise Unsafe('missing or invalid temperature')
            channels = {c['name']: c for c in latest['channels']}
            for device_uid, name in controlled:
                if device_uid == uid and name not in channels:
                    raise Unsafe('missing controlled channel')
        self.observed = observed
        self.plan, self.expected_mask = plan, expected_mask
        return output_healthy

    def replay(self):
        if not self.plan or self.observed is None:
            raise Unsafe('no validated recovery snapshot')
        # Bounded total budget; the startup lease is never refreshed here.
        self.api.begin(20)
        expected = copy.deepcopy(self.observed)
        for index, entry in enumerate(self.plan):
            if self.read_settings() != expected:
                raise Unsafe('channel settings changed before replay operation')
            base = '/devices/' + quoted(entry['device_uid']) + '/settings/' + quoted(entry['channel_name'])
            target = entry['setting']
            software = self.expected_mask & (1 << (entry['pwm'] - 1))
            # Avoid unnecessary writes for already-correct unmanaged channels.
            if not software and expected[index]['setting'] == target:
                continue
            self.api.request('PUT', base + '/reset')
            expected[index]['setting'] = {'reset_to_default': True}
            if self.read_settings() != expected:
                raise Unsafe('channel settings changed after reset')
            if 'profile_uid' in target:
                self.api.request('PUT', base + '/profile', target)
            elif 'speed_fixed' in target:
                self.api.request('PUT', base + '/manual', target)
            expected[index]['setting'] = copy.deepcopy(target)
            if self.read_settings() != expected:
                raise Unsafe('channel setting replay was not confirmed')

    def control_matches(self, status, baseline=None, expected_mask=None):
        expected = self.expected_mask if expected_mask is None else expected_mask
        if status['manual_mask'] != expected:
            return False
        return baseline is None or all(
            not (expected & (1 << (c['pwm'] - 1))) or
            status['writes' + str(c['pwm'])] > baseline['writes' + str(c['pwm'])]
            for c in self.cfg['channels'])


class Keeper:
    def __init__(self, config, driver, health, state_path, clock=time.monotonic):
        self.cfg, self.driver, self.health, self.clock = config, driver, health, clock
        self.state_path = Path(state_path)
        self.attempts = 0
        self.snapshot_scope = {'api_url': config['api_url'], 'channels': config['channels']}
        self.settings_snapshot = None
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text())
            self.attempts = int(saved['attempts'])
            if saved.get('snapshot_scope') == self.snapshot_scope:
                snapshot = saved.get('settings_snapshot')
                if snapshot is not None:
                    self.settings_snapshot = self.health.validate_settings(snapshot)
            if not 0 <= self.attempts <= 3:
                raise Unsafe('invalid persistent attempt counter')
        self.phase = 'locked'
        self.stable_since = self.running_since = None
        self.next_attempt = clock() + (60 if self.attempts else 0)
        self.start_deadline = None
        self.baseline = None
        self.authorization_retry_at = self.authorization_deadline = None
        self.last_event = None
        # Never inherit an old active lease after keeper restart.
        self.driver.fallback()

    def event(self, message):
        if message != self.last_event:
            logging.warning(message)
            self.last_event = message

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump({'attempts': self.attempts, 'settings_snapshot': self.settings_snapshot,
                       'snapshot_scope': self.snapshot_scope}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)
        fd = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def remember_settings(self, settings):
        if settings != self.settings_snapshot:
            self.settings_snapshot = copy.deepcopy(settings)
            self.save()

    def trip(self, reason):
        self.phase = 'locked'
        self.stable_since = self.running_since = None
        self.next_attempt = max(self.next_attempt, self.clock() + 60)
        try:
            self.driver.fallback()
        except OSError:
            pass  # No further renewal: kernel deadline remains the backstop.
        self.event(reason)

    def authorize(self, allow_busy_retry):
        # Re-read after HTTP checks; never authorize using an old status snapshot.
        current = self.driver.status()
        if (current['state'] != 'locked' or current['manual_mask'] or
                current['failed_mask'] or current['generation'] != self.baseline['generation']):
            raise Unsafe('authorization precondition changed; re-observe automatic mode')
        started = self.clock()
        try:
            self.driver.arm(current['generation'], 60)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise  # Uncertain outcomes are not safe to retry.
            fresh = self.driver.status()
            if (fresh['state'] != 'locked' or fresh['manual_mask'] or
                    fresh['failed_mask'] or fresh['generation'] != current['generation']):
                raise Unsafe('authorization refused: automatic mode is not stable') from exc
            if not allow_busy_retry:
                raise Unsafe('authorization remained busy after one bounded retry') from exc
            # One delayed retry shares the already-persisted attempt. A restart cannot
            # erase that attempt. No lease exists during this wait.
            self.phase = 'authorizing'
            self.authorization_retry_at = self.clock() + 3
            self.authorization_deadline = self.clock() + 15
            self.event('authorization busy; automatic mode rechecked, one retry scheduled')
            return
        self.baseline = current
        self.recovery_mask = self.health.expected_mask
        self.recovery_settings = copy.deepcopy(self.health.plan)
        self.start_deadline = started + 60
        self.phase = 'starting'
        self.event('control authorized; replaying protected channel snapshot')
        self.health.replay()

    def step(self):
        try:
            s = self.driver.status()
            if s['state'] == 'failed' or s['failed_mask']:
                raise Unsafe('kernel recovery failed; manual intervention required')
            recovering = self.phase in ('locked', 'authorizing', 'starting')
            output_healthy = self.health.snapshot(locked=recovering,
                                                  recovery_settings=self.settings_snapshot if recovering else None)
            now = self.clock()
            if self.phase == 'authorizing':
                if now >= self.authorization_deadline:
                    raise Unsafe('authorization retry observation window expired')
                if now >= self.authorization_retry_at:
                    self.authorize(allow_busy_retry=False)
                return
            if self.phase == 'locked':
                if s['state'] != 'locked' or s['manual_mask']:
                    raise Unsafe('driver is not safely locked in automatic mode')
                self.remember_settings(self.health.plan)
                if self.stable_since is None:
                    self.stable_since = now
                if self.attempts >= 3:
                    self.event('three recovery attempts exhausted; manual intervention required')
                    return
                if now - self.stable_since < 30 or now < self.next_attempt:
                    return
                self.attempts += 1
                self.save()  # Persist before authorization; restart cannot erase the budget.
                self.baseline = s
                self.authorize(allow_busy_retry=True)
                return
            if (s['state'] != 'active' or s['generation'] != self.baseline['generation'] or
                    s['remaining_ms'] <= 0):
                raise Unsafe('lease lost or generation changed')
            if self.phase == 'starting':
                if now >= self.start_deadline:
                    raise Unsafe('recovery deadline expired')
                if (self.health.observed != self.recovery_settings or
                        self.health.expected_mask != self.recovery_mask):
                    raise Unsafe('recovery settings changed before verification')
                if not output_healthy or not self.health.control_matches(s, self.baseline, self.recovery_mask):
                    return
                self.phase, self.running_since = 'running', now
                self.event('lease active; channel snapshot control verified')
            # UI settings may update before the asynchronous hardware writer.
            # Wait within the existing lease, without renewing or replacing the saved snapshot.
            fresh = self.driver.status()
            if (fresh['state'] != 'active' or fresh['generation'] != self.baseline['generation'] or
                    fresh['remaining_ms'] <= 0):
                raise Unsafe('lease lost before renewal')
            if not self.health.control_matches(fresh):
                self.event('waiting for hardware to match channel settings; lease not renewed')
                return
            self.driver.renew(15)
            self.remember_settings(self.health.plan)
            if self.attempts and now - self.running_since >= 600:
                self.attempts = 0
                self.save()
        except (Unsafe, OSError, HTTPException, ValueError, KeyError, TypeError, IndexError) as exc:
            # Never log an HTTP exception's URL, response body or credentials.
            if isinstance(exc, HTTPError):
                reason = f'HTTPError status={int(exc.code)}'
            else:
                reason = str(exc) if isinstance(exc, Unsafe) else type(exc).__name__
            self.trip('fallback: ' + reason)


def validate(config):
    channels = config['channels']
    if not channels or len(channels) > 8:
        raise Unsafe('one to eight protected channels required')
    pwms = [c['pwm'] for c in channels]
    if any(type(x) is not int or not 1 <= x <= 8 for x in pwms) or len(set(pwms)) != len(pwms):
        raise Unsafe('invalid or duplicate PWM mapping')
    if len({(c['device_uid'], c['channel_name']) for c in channels}) != len(channels):
        raise Unsafe('duplicate CoolerControl channel')
    if len({c['device_uid'] for c in channels}) != 1:
        raise Unsafe('one nct6687 controller per sidecar')
    if not config['required_sources']:
        raise Unsafe('explicit required physical temperature sources are required')
    return sum(1 << (x - 1) for x in pwms)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/config/config.json')
    parser.add_argument('--state', default='/state/attempts.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(Path(args.config).read_text())
    mask = validate(config)
    state_dir = Path(args.state).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / 'keeper.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    driver = Driver(config['driver_path'], mask)
    keeper = Keeper(config, driver, Health(config, API(config)), args.state)
    stop = False

    def stopping(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        while not stop:
            started = time.monotonic()
            keeper.step()
            time.sleep(max(0, 3 - (time.monotonic() - started)))
    finally:
        driver.fallback()
        lock.close()


if __name__ == '__main__':
    main()
