"""Disposable metadata tests only: no keychain, phone, or real signing profile."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import signing_preflight as subject


class SigningPreflightTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.app = Path(self.sandbox.name) / 'Sample.app'
        self.app.mkdir()
        self.info = {'CFBundleIdentifier': 'example.pocket'}
        (self.app / 'Info.plist').write_bytes(plistlib.dumps(self.info))
        self.now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.profile = {
            'Entitlements': {'application-identifier': 'PRIVATE.example.pocket', 'get-task-allow': True},
            'ProvisionedDevices': ['PRIVATE_DEVICE'],
            'TeamIdentifier': ['PRIVATE_TEAM'],
            'DeveloperCertificates': [b'PRIVATE_CERTIFICATE'],
            'ExpirationDate': self.now.replace(tzinfo=None) + timedelta(days=3, hours=12),
        }

    def analyze(self, profile=None):
        return subject.analyze_profile(self.info, self.profile if profile is None else profile, self.now)

    def test_development_expiry_and_privacy(self):
        report = self.analyze()
        self.assertEqual(report['signingType'], 'development_profile')
        self.assertEqual(report['daysRemaining'], 3.5)
        self.assertFalse(report['expired'])
        self.assertTrue(report['bundleIdentifierMatchesProfile'])
        rendered = json.dumps(report)
        for private in ['PRIVATE', 'example.pocket']:
            self.assertNotIn(private, rendered)

    def test_expired_and_exact_boundary(self):
        for delta in [timedelta(days=-1), timedelta(0)]:
            self.profile['ExpirationDate'] = self.now + delta
            self.assertTrue(self.analyze()['expired'])

    def test_wildcard_bundle_and_mismatch(self):
        self.profile['Entitlements']['application-identifier'] = 'PRIVATE.example.*'
        self.assertTrue(self.analyze()['bundleIdentifierMatchesProfile'])
        self.profile['Entitlements']['application-identifier'] = 'PRIVATE.other.*'
        self.assertEqual(self.analyze()['status'], 'bundle_profile_mismatch')

    def test_all_apps_wildcard(self):
        self.profile['Entitlements']['application-identifier'] = 'PRIVATE.*'
        self.assertTrue(self.analyze()['bundleIdentifierMatchesProfile'])

    def test_missing_expiry_is_neutral_error(self):
        del self.profile['ExpirationDate']
        with self.assertRaises(subject.PreflightError):
            self.analyze()

    def test_profile_types(self):
        self.profile['Entitlements']['get-task-allow'] = False
        self.assertEqual(self.analyze()['signingType'], 'ad_hoc_profile')
        del self.profile['ProvisionedDevices']
        self.assertEqual(self.analyze()['signingType'], 'distribution_profile_without_device_list')
        self.profile['ProvisionsAllDevices'] = True
        self.assertEqual(self.analyze()['signingType'], 'enterprise_profile')

    def test_missing_simulator_profile_does_not_run_security(self):
        with patch.object(subject.subprocess, 'run') as run:
            report = subject.inspect_app(self.app, now=self.now)
        run.assert_not_called()
        self.assertEqual(report['status'], 'no_device_signing_evidence')

    def test_profile_inspection_uses_only_explicit_app(self):
        (self.app / 'embedded.mobileprovision').write_bytes(b'disposable CMS fixture')
        with patch.object(subject, 'decode_profile', return_value=self.profile) as decoder:
            self.assertEqual(subject.inspect_app(self.app, now=self.now)['status'], 'profile_inspected')
        decoder.assert_called_once_with(self.app.resolve() / 'embedded.mobileprovision')

    def test_rejects_bad_info_and_wrong_target(self):
        (self.app / 'Info.plist').write_bytes(b'not a plist')
        with self.assertRaises(subject.PreflightError):
            subject.inspect_app(self.app)
        with self.assertRaises(subject.PreflightError):
            subject.inspect_app(self.app.parent)

    def test_cms_output_and_errors_are_not_exposed(self):
        profile = self.app / 'embedded.mobileprovision'
        profile.write_bytes(b'disposable CMS fixture')
        failure = subject.subprocess.CompletedProcess([], 1, b'PRIVATE_DATA', b'PRIVATE_ACCOUNT')
        with patch.object(subject.subprocess, 'run', return_value=failure):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(subject.main(['--app', str(self.app)]), 1)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn(str(self.app), output.getvalue())

    def test_cms_decodes_synthetic_plist(self):
        profile = self.app / 'embedded.mobileprovision'
        profile.write_bytes(b'disposable CMS fixture')
        success = subject.subprocess.CompletedProcess([], 0, plistlib.dumps(self.profile), b'')
        with patch.object(subject.subprocess, 'run', return_value=success) as run:
            self.assertEqual(subject.decode_profile(profile), self.profile)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ['/usr/bin/security', 'cms', '-D', '-i'])

    def test_requires_explicit_app(self):
        with redirect_stdout(io.StringIO()), patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                subject.main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
