"""Native boundary tests with a fake AFC; never load a framework or a device."""
from __future__ import annotations

import ctypes as C
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pocket_device_native as native

STAGE = ".stillroom-" + "a" * 32 + ".partial"
PAYLOAD = STAGE + "/library.partial"
FINAL = "Stillroom-" + "a" * 32 + ".stillroomlibrary"


class FakeAFC:
    def __init__(self):
        self.fs = {"Documents": {"type": "directory", "size": 0}}
        self.data = {}; self.handles = {}; self.directories = {}; self.next = 1
        self.opens = 0; self.afc_closed = 0; self.mutations = []
    def mkdir(self, connection, path):
        path = path.decode()
        if path in self.fs: return 16
        self.mutations.append(("mkdir", path)); self.fs[path] = {"type": "directory", "size": 0}; return 0
    def file_open(self, connection, path, mode, output):
        path = path.decode()
        if mode == 1 and path not in self.fs: return 8
        if mode == 3:
            self.mutations.append(("open_write", path)); self.data[path] = b""; self.fs[path] = {"type": "file", "size": 0}
        handle = self.next; self.next += 1; self.handles[handle] = [path, 0]; output._obj.value = handle; self.opens += 1; return 0
    def file_write(self, connection, handle, buffer, count):
        path, position = self.handles[handle]
        chunk = C.string_at(buffer, count); previous = self.data[path]
        self.data[path] = previous[:position] + chunk + previous[position + count:]
        self.handles[handle][1] += count; self.fs[path]["size"] = len(self.data[path]); return 0
    def file_read(self, connection, handle, buffer, length):
        path, position = self.handles[handle]; value = self.data[path][position:position + length._obj.value]
        C.memmove(buffer, value, len(value)); length._obj.value = len(value); self.handles[handle][1] += len(value); return 0
    def file_seek(self, connection, handle, offset, whence): self.handles[handle][1] = offset; return 0
    def file_close(self, connection, handle): self.handles.pop(handle, None); return 0
    def rename(self, connection, old, new):
        old, new = old.decode(), new.decode(); self.mutations.append(("rename", old, new))
        self.fs[new] = self.fs.pop(old); self.data[new] = self.data.pop(old); return 0
    def remove(self, connection, path):
        path = path.decode(); self.mutations.append(("remove", path)); self.fs.pop(path, None); self.data.pop(path, None); return 0
    def dir_open(self, connection, path, output):
        prefix = path.decode() + "/"; names = [".", ".."] + [name[len(prefix):] for name in self.fs if name.startswith(prefix) and "/" not in name[len(prefix):]]
        handle = self.next; self.next += 1; self.directories[handle] = iter(names); output._obj.value = handle; return 0
    def dir_read(self, connection, directory, output):
        name = next(self.directories[directory.value], None); output._obj.value = name.encode() if name is not None else None; return 0
    def dir_close(self, connection, directory): self.directories.pop(directory.value, None); return 0
    def afc_close(self, connection): self.afc_closed += 1; return 0


class Session(native.NativeSession):
    def _raw_stat(self, path):
        info = self._api.fs.get(path)
        return dict(info) if info is not None else None


class NativeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAFC(); self.backend = SimpleNamespace(_sessions=set())
        self.session = Session(self.api, 1, self.backend); self.backend._sessions.add(self.session)
    def tearDown(self): self.session.close()
    def write(self, value=b"original compressed bytes"):
        self.session.mkdir(STAGE); handle = self.session.open_write(PAYLOAD)
        self.session.write(handle, value); self.session.close_file(handle); return value

    def test_path_validation_never_escapes_documents_or_accepts_symlink_parents(self):
        for path in ["", "/Documents/a", "../a", "a/../b", "a//b", "a\\b", "a\x00b", "a:b", ".", "a/b/c/d", "bad\nname"]:
            with self.subTest(path=path), self.assertRaises(native.NativeError): self.session.stat(path)
        self.api.fs["Documents/link"] = {"type": "symlink", "size": 0}
        with self.assertRaises(native.NativeError): self.session.stat("link/secret")
        with self.assertRaises(native.NativeError): self.session.remove("link")
        self.assertEqual(self.api.mutations, [])

    def test_documents_boundary_must_be_a_real_directory(self):
        self.api.fs["Documents"]["type"] = "symlink"
        with self.assertRaises(native.NativeError): Session(self.api, 2, self.backend)
        with self.assertRaises(native.NativeError): self.session.stat("anything")
        self.assertEqual(self.api.mutations, [])

    def test_only_new_uuid_stage_can_be_created_and_existing_stage_is_never_adopted(self):
        with self.assertRaises(native.NativeError): self.session.mkdir("ordinary-directory")
        self.api.fs["Documents/" + STAGE] = {"type": "directory", "size": 0}
        with self.assertRaises(native.NativeError) as error: self.session.mkdir(STAGE)
        self.assertEqual(error.exception.code, "exists"); self.assertEqual(self.session._owned_dirs, set())
        with self.assertRaises(native.NativeError): self.session.open_write(PAYLOAD)
        with self.assertRaises(native.NativeError): self.session.remove(STAGE)
        self.assertEqual(self.api.mutations, [])

    def test_write_is_owned_bounded_and_never_truncates_an_existing_payload(self):
        with self.assertRaises(native.NativeError): self.session.open_write("existing")
        value = self.write()
        with self.assertRaises(native.NativeError) as error: self.session.open_write(PAYLOAD)
        self.assertEqual(error.exception.code, "exists")
        self.assertEqual(self.api.data["Documents/" + PAYLOAD], value)
        with self.assertRaises(native.NativeError): self.session.write(999, b"bad")
        self.session.mkdir(".stillroom-" + "b" * 32 + ".partial")
        handle = self.session.open_write(".stillroom-" + "b" * 32 + ".partial/library.partial")
        with self.assertRaises(native.NativeError): self.session.write(handle, b"x" * (native.MAX_CHUNK + 1))
        with self.assertRaises(native.NativeError): self.session.write(True, b"bad")
        self.session.close_file(handle)

    def test_readback_uses_one_cached_handle_and_can_seek_without_allocating_song_buffers(self):
        value = self.write()
        self.assertEqual(self.session.read(PAYLOAD, 0, 10), value[:10])
        self.assertEqual(self.session.read(PAYLOAD, 10, 15), value[10:])
        self.assertEqual(self.api.opens, 2, "one writer and one reused reader")
        self.assertEqual(self.session.read(PAYLOAD, 0, 8), value[:8])
        self.assertEqual(self.session.read(PAYLOAD, len(value), 1), b"")
        for offset, size in [(-1, 10), (0, native.MAX_CHUNK + 1), (True, 1)]:
            with self.subTest(offset=offset, size=size), self.assertRaises(native.NativeError): self.session.read(PAYLOAD, offset, size)

    def test_publication_refuses_existing_files_and_cleanup_never_removes_unknown_files(self):
        value = self.write(); self.session.read(PAYLOAD, 0, 2)
        self.api.fs["Documents/existing.stillroomlibrary"] = {"type": "file", "size": 7}
        with self.assertRaises(native.NativeError) as error: self.session.rename(PAYLOAD, "existing.stillroomlibrary")
        self.assertEqual(error.exception.code, "exists")
        self.session.rename(PAYLOAD, FINAL)
        self.assertIsNone(self.session.stat(PAYLOAD)); self.assertEqual(self.session.read(FINAL, 0, len(value)), value)
        self.assertEqual(self.session.listdir(STAGE), []); self.session.remove(STAGE)
        self.assertIsNone(self.session.stat(STAGE))
        with self.assertRaises(native.NativeError): self.session.remove("existing.stillroomlibrary")
        self.assertIn("Documents/existing.stillroomlibrary", self.api.fs)

    def test_close_releases_handles_but_never_deletes_published_or_incomplete_files(self):
        value = self.write(); self.session.rename(PAYLOAD, FINAL)
        stage = ".stillroom-" + "b" * 32 + ".partial"; self.session.mkdir(stage)
        handle = self.session.open_write(stage + "/incomplete"); self.session.write(handle, b"part")
        self.session.close(); self.session.close()
        self.assertEqual(self.api.afc_closed, 1); self.assertEqual(self.api.handles, {})
        self.assertIn("Documents/" + stage, self.api.fs); self.assertIn("Documents/" + FINAL, self.api.fs)
        self.assertEqual(self.backend._sessions, set())
        with self.assertRaises(native.NativeError): self.session.stat(FINAL)

    def test_status_codes_return_constant_safe_errors(self):
        for status, code in [(8, "not_found"), (16, "exists"), (18, "no_space"), (999, "io_error")]:
            with self.subTest(status=status), self.assertRaises(native.NativeError) as error: native._afc_check(status)
            self.assertEqual(error.exception.code, code); self.assertNotIn(str(status), str(error.exception))
        native._afc_check(0)


if __name__ == "__main__": unittest.main()
