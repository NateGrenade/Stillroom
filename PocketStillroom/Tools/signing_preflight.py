#!/usr/bin/env python3
"""Offline summary of one explicitly selected .app's embedded signing profile.

Never queries the keychain, devices, network, or signing services. This is not a
signature/trust check and does not establish the state of an installed phone app.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import sys

MAX_INFO_BYTES = 1024 * 1024
MAX_PROFILE_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024


class PreflightError(Exception):
    """A fixed, non-sensitive diagnostic suitable for the report."""


def read_plist(path: Path, limit: int) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise PreflightError('App metadata is missing or exceeds the inspection limit.')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PreflightError('App metadata exceeds the inspection limit.')
        value = plistlib.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except PreflightError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException, OverflowError):
        raise PreflightError('App metadata could not be read as a property list.') from None


def decode_profile(path: Path) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > MAX_PROFILE_BYTES:
            raise PreflightError('Embedded profile exceeds the inspection limit or is unreadable.')
        result = subprocess.run(
            ['/usr/bin/security', 'cms', '-D', '-i', str(path)],
            capture_output=True, timeout=15, check=False,
        )
        # CMS output and diagnostics can contain identifiers: never echo them.
        if result.returncode or len(result.stdout) > MAX_DECODED_BYTES:
            raise PreflightError('Embedded profile could not be decoded within inspection limits.')
        value = plistlib.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except PreflightError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException,
            OverflowError, subprocess.TimeoutExpired):
        raise PreflightError('Embedded profile could not be decoded within inspection limits.') from None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bundle_matches(bundle_id: str, application_id: str) -> bool:
    # Only inspect the app portion. Never return the account/team prefix.
    if '.' not in application_id:
        return False
    pattern = application_id.split('.', 1)[1]
    if pattern == '*':
        return True
    if pattern.endswith('.*') and '*' not in pattern[:-1]:
        return bundle_id.startswith(pattern[:-1]) and len(bundle_id) > len(pattern[:-1])
    return '*' not in pattern and bundle_id == pattern


def analyze_profile(info: dict, profile: dict | None, now: datetime) -> dict:
    report = {
        'schemaVersion': 1,
        'inspection': 'offline_explicit_app_only',
        'status': 'no_device_signing_evidence',
        'signingType': 'not_established',
        'bundleIdentifierMatchesProfile': None,
        'expiresAtUTC': None,
        'daysRemaining': None,
        'expired': None,
        'limitations': [
            'Describes this saved app only, not the installed phone app.',
            'Does not verify signature, certificate trust, revocation, or install eligibility.',
            'Does not identify an account, team, phone, certificate, or key.',
        ],
    }
    if profile is None:
        report['note'] = 'No embedded device profile; expected for simulator builds and some other signing arrangements.'
        return report
    entitlements = profile.get('Entitlements')
    expiry = profile.get('ExpirationDate')
    if not isinstance(entitlements, dict) or not isinstance(expiry, datetime):
        raise PreflightError('Embedded profile lacks usable entitlements or expiry metadata.')
    bundle_id = info.get('CFBundleIdentifier')
    application_id = entitlements.get('application-identifier')
    if not isinstance(bundle_id, str) or not bundle_id or not isinstance(application_id, str):
        raise PreflightError('App or embedded profile lacks a usable bundle identifier.')
    devices = profile.get('ProvisionedDevices')
    if profile.get('ProvisionsAllDevices') is True:
        signing_type = 'enterprise_profile'
    elif isinstance(devices, list) and devices:
        signing_type = 'development_profile' if entitlements.get('get-task-allow') is True else 'ad_hoc_profile'
    elif 'ProvisionedDevices' not in profile and entitlements.get('get-task-allow') is False:
        signing_type = 'distribution_profile_without_device_list'
    else:
        signing_type = 'profile_type_not_established'
    expiry = _utc(expiry)
    seconds = (expiry - _utc(now)).total_seconds()
    matches = _bundle_matches(bundle_id, application_id)
    report.update({
        'status': 'profile_inspected' if matches else 'bundle_profile_mismatch',
        'signingType': signing_type,
        'bundleIdentifierMatchesProfile': matches,
        'expiresAtUTC': expiry.isoformat().replace('+00:00', 'Z'),
        'daysRemaining': round(seconds / 86400, 3),
        'expired': seconds <= 0,
    })
    return report


def inspect_app(app: Path, *, now: datetime | None = None) -> dict:
    app = app.expanduser().resolve()
    if app.suffix.lower() != '.app' or not app.is_dir():
        raise PreflightError('Select one existing .app directory with --app.')
    info = read_plist(app / 'Info.plist', MAX_INFO_BYTES)
    embedded = app / 'embedded.mobileprovision'
    profile = decode_profile(embedded) if embedded.exists() else None
    return analyze_profile(info, profile, now or datetime.now(timezone.utc))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path, help='One saved iOS or simulator .app directory')
    args = parser.parse_args(argv)
    try:
        report = inspect_app(args.app)
    except PreflightError as exc:
        print(json.dumps({'status': 'inspection_error', 'reason': str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 2 if report['status'] == 'bundle_profile_mismatch' else 0


if __name__ == '__main__':
    sys.exit(main())
