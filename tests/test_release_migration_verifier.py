"""Protect release migration QA against data loss and additive API false alarms."""
import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    'release_migration_verifier', Path(__file__).resolve().parents[1] / 'packaging' / 'verify_update_package.py')
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class TrackMetadataPreservationTests(unittest.TestCase):
    def setUp(self):
        self.before = [{'id': 'one', 'title': 'Saved title', 'artist': '', 'loopEnd': None}]

    def test_missing_storage_may_become_null(self):
        after = copy.deepcopy(self.before)
        after[0]['storage'] = None
        self.assertTrue(VERIFIER.track_metadata_preserved(self.before, after))
        self.assertNotIn('storage', self.before[0])

    def test_unchanged_older_api_shape_is_allowed(self):
        self.assertTrue(VERIFIER.track_metadata_preserved(self.before, copy.deepcopy(self.before)))

    def test_changed_existing_field_is_rejected(self):
        for field, value in [('id', 'replacement'), ('title', 'Changed'), ('artist', 'Other'), ('loopEnd', 4)]:
            with self.subTest(field=field):
                after = copy.deepcopy(self.before)
                after[0][field] = value
                self.assertFalse(VERIFIER.track_metadata_preserved(self.before, after))

    def test_deleted_existing_field_including_null_is_rejected(self):
        for field in self.before[0]:
            with self.subTest(field=field):
                after = copy.deepcopy(self.before)
                del after[0][field]
                self.assertFalse(VERIFIER.track_metadata_preserved(self.before, after))

    def test_added_nonnull_storage_is_rejected(self):
        for value in [{}, {'codec': 'aac'}, '', 0, False]:
            with self.subTest(value=value):
                after = copy.deepcopy(self.before)
                after[0]['storage'] = value
                self.assertFalse(VERIFIER.track_metadata_preserved(self.before, after))

    def test_unexpected_added_field_is_rejected(self):
        after = copy.deepcopy(self.before)
        after[0]['unexpected'] = None
        self.assertFalse(VERIFIER.track_metadata_preserved(self.before, after))

    def test_existing_storage_must_match_exactly(self):
        for storage in [None, {'source': {'sha256': 'synthetic'}, 'transformation': 'preserved'}]:
            with self.subTest(storage=storage):
                before = copy.deepcopy(self.before)
                before[0]['storage'] = storage
                self.assertTrue(VERIFIER.track_metadata_preserved(before, copy.deepcopy(before)))
                after = copy.deepcopy(before)
                del after[0]['storage']
                self.assertFalse(VERIFIER.track_metadata_preserved(before, after))
                after = copy.deepcopy(before)
                after[0]['storage'] = {'different': True}
                self.assertFalse(VERIFIER.track_metadata_preserved(before, after))

    def test_existing_numeric_field_cannot_become_boolean(self):
        before = copy.deepcopy(self.before)
        before[0]['duration'] = 1.0
        after = copy.deepcopy(before)
        after[0]['duration'] = True
        self.assertFalse(VERIFIER.track_metadata_preserved(before, after))

    def test_nested_storage_boolean_cannot_become_number(self):
        before = copy.deepcopy(self.before)
        before[0]['storage'] = {'source': {'bitrateEstimated': False}}
        after = copy.deepcopy(before)
        after[0]['storage']['source']['bitrateEstimated'] = 0
        self.assertFalse(VERIFIER.track_metadata_preserved(before, after))

    def test_track_count_and_order_are_preserved(self):
        second = {'id': 'two', 'title': 'Second'}
        before = self.before + [second]
        self.assertFalse(VERIFIER.track_metadata_preserved(before, self.before))
        self.assertFalse(VERIFIER.track_metadata_preserved(self.before, before))
        self.assertFalse(VERIFIER.track_metadata_preserved(before, list(reversed(before))))


if __name__ == '__main__':
    unittest.main()
