import hashlib
import os
import stat
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TempDirCase, importer


def write(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run(inputs, **options):
    issues = []
    assets = list(importer.import_files(inputs, issues, **options))
    return assets, issues


def codes(issues):
    return [issue["code"] for issue in issues]


class FingerprintTests(TempDirCase):
    def test_hash_and_identity_match_the_file(self):
        data = bytes(range(256)) * 3000
        path = write(self.tmp / "a.mp4", data)
        asset = importer.fingerprint_file(path, "a.mp4")
        info = path.stat()
        self.assertEqual(asset["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(
            asset,
            {
                "path": str(path),
                "relative_path": "a.mp4",
                "size_bytes": len(data),
                "mtime_ns": info.st_mtime_ns,
                "sha256": asset["sha256"],
            },
        )

    def test_empty_file_is_a_valid_asset(self):
        asset = importer.fingerprint_file(write(self.tmp / "e.mp4", b""), "e.mp4")
        self.assertEqual(asset["sha256"], hashlib.sha256(b"").hexdigest())

    def test_rejects_folders_and_missing_files(self):
        with self.assertRaises(ValueError):
            importer.fingerprint_file(self.tmp, "x")
        with self.assertRaises(OSError):
            importer.fingerprint_file(self.tmp / "missing.mp4", "x")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_named_pipe_is_refused_without_blocking(self):
        fifo = self.tmp / "pipe.mp4"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            importer.fingerprint_file(fifo, "pipe.mp4")

    def test_modification_while_hashing_is_detected(self):
        path = write(self.tmp / "a.mp4", b"x" * 1000)
        real = hashlib.file_digest

        def tamper(stream, name):
            digest = real(stream, name)
            with open(path, "ab") as other:
                other.write(b"more")
            return digest

        with mock.patch.object(importer.hashlib, "file_digest", tamper):
            with self.assertRaisesRegex(ValueError, "changed"):
                importer.fingerprint_file(path, "a.mp4")


class DiscoveryTests(TempDirCase):
    def tree(self):
        for name, data in (
            ("a/x.MP4", b"1"), ("a/y.txt", b"2"), ("a/b/z.avi", b"3"), ("a/b/c/w.mkv", b"4"),
        ):
            write(self.tmp / name, data)
        return self.tmp / "a"

    def test_folder_search_is_recursive_filtered_and_sorted(self):
        assets, issues = run([self.tree()])
        self.assertEqual([a["relative_path"] for a in assets], ["b/c/w.mkv", "b/z.avi", "x.MP4"])
        self.assertEqual(issues, [])

    def test_extension_filter_accepts_dotless_and_any_case(self):
        assets, _ = run([self.tree()], extensions=["MP4", ".Avi"])
        self.assertEqual([a["relative_path"] for a in assets], ["b/z.avi", "x.MP4"])

    def test_star_accepts_every_file(self):
        assets, _ = run([self.tree()], extensions=["*"])
        self.assertEqual(len(assets), 4)

    def test_explicit_file_is_imported_whatever_its_extension(self):
        assets, _ = run([self.tree() / "y.txt"])
        self.assertEqual([a["relative_path"] for a in assets], ["y.txt"])

    def test_several_inputs_get_unique_paths_below_their_common_root(self):
        write(self.tmp / "d1/x.mp4", b"1")
        write(self.tmp / "d2/x.mp4", b"2")
        assets, _ = run([self.tmp / "d2", self.tmp / "d1"])
        self.assertEqual([a["relative_path"] for a in assets], ["d1/x.mp4", "d2/x.mp4"])

    def test_a_file_named_twice_is_imported_once(self):
        folder = self.tree()
        assets, _ = run([folder, folder / "x.MP4", folder])
        self.assertEqual(len(assets), 3)

    def test_identical_content_is_reported_but_both_files_are_kept(self):
        write(self.tmp / "a.mp4", b"same")
        write(self.tmp / "b.mp4", b"same")
        assets, issues = run([self.tmp])
        self.assertEqual(len(assets), 2)
        self.assertEqual(codes(issues), ["duplicate_content"])
        self.assertIn("a.mp4", issues[0]["message"])
        self.assertTrue(issues[0]["path"].endswith("b.mp4"))

    def test_unusual_names_are_imported(self):
        for name in ("-leading.mp4", "ünï cödé 视频.mp4", "with space.mp4"):
            write(self.tmp / name, name.encode())
        assets, issues = run([self.tmp])
        self.assertEqual(len(assets), 3)
        self.assertEqual(issues, [])

    def test_nothing_found_is_an_error(self):
        assets, issues = run([self.tmp])
        self.assertEqual((assets, codes(issues)), ([], ["no_inputs"]))
        self.assertEqual(issues[0]["severity"], "error")

    def test_missing_input_is_reported_and_others_still_import(self):
        write(self.tmp / "ok.mp4")
        assets, issues = run([self.tmp / "nope.mp4", self.tmp / "ok.mp4"])
        self.assertEqual(len(assets), 1)
        self.assertEqual(codes(issues), ["input_not_found"])

    def test_unreadable_folder_is_reported(self):
        def failing_walk(top, onerror=None, **_):
            onerror(PermissionError(13, "Permission denied", str(top / "locked")))
            return iter(())

        with mock.patch.object(importer.os, "walk", failing_walk):
            _, issues = run([self.tmp])
        self.assertIn("folder_unreadable", codes(issues))
        self.assertTrue(issues[0]["path"].endswith("locked"))

    def test_unreadable_file_is_reported_and_the_rest_continue(self):
        write(self.tmp / "a.mp4", b"1")
        write(self.tmp / "b.mp4", b"2")
        real = importer.fingerprint_file

        def flaky(path, relative):
            if relative == "a.mp4":
                raise PermissionError(13, "denied", str(path))
            return real(path, relative)

        with mock.patch.object(importer, "fingerprint_file", flaky):
            assets, issues = run([self.tmp])
        self.assertEqual([a["relative_path"] for a in assets], ["b.mp4"])
        self.assertEqual(codes(issues), ["file_unreadable"])

    def test_hashing_is_lazy_one_file_per_step(self):
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            write(self.tmp / name, name.encode())
        calls = []
        real = importer.fingerprint_file

        def counting(path, relative):
            calls.append(relative)
            return real(path, relative)

        with mock.patch.object(importer, "fingerprint_file", counting):
            stream = importer.import_files([self.tmp], [])
            next(stream)
            self.assertEqual(calls, ["a.mp4"])


class EdgeCases(TempDirCase):
    def test_a_path_that_cannot_be_inspected_is_not_a_link(self):
        with mock.patch.object(importer.Path, "lstat", side_effect=PermissionError):
            self.assertIs(importer._is_link(self.tmp), False)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_inputs_that_are_neither_files_nor_folders_are_reported(self):
        os.mkfifo(self.tmp / "pipe.mp4")
        assets, issues = run([self.tmp / "pipe.mp4"])
        self.assertEqual((assets, codes(issues)), ([], ["input_unsupported", "no_inputs"]))

    def test_inputs_without_a_common_root_keep_absolute_names(self):
        write(self.tmp / "a/x.mp4", b"1")
        write(self.tmp / "b/y.mp4", b"2")
        with mock.patch.object(importer.os.path, "commonpath", side_effect=ValueError("different drives")):
            assets, _ = run([self.tmp / "a", self.tmp / "b"])
        self.assertEqual([a["relative_path"] for a in assets], [(self.tmp / "a/x.mp4").resolve().as_posix(),
                                                               (self.tmp / "b/y.mp4").resolve().as_posix()])

    def test_windows_mount_points_count_as_links_but_other_reparse_points_do_not(self):
        def inspect(mode, tag=None):
            info = types.SimpleNamespace(st_mode=mode)
            if tag is not None:
                info.st_reparse_tag = tag
            with mock.patch.object(importer.Path, "lstat", return_value=info):
                return importer._is_link(self.tmp)

        self.assertIs(inspect(stat.S_IFDIR, 0xA0000003), True)  # IO_REPARSE_TAG_MOUNT_POINT (junction)
        self.assertIs(inspect(stat.S_IFDIR, 0x9000001A), False)  # e.g. a cloud-storage placeholder
        self.assertIs(inspect(stat.S_IFDIR, 0), False)
        self.assertIs(inspect(stat.S_IFDIR), False)  # POSIX: the attribute does not exist
        self.assertIs(inspect(stat.S_IFLNK), True)
        self.assertIs(inspect(stat.S_IFREG), False)

    def test_junctions_are_never_descended_into(self):
        write(self.tmp / "data/junction/inside.mp4", b"1")
        write(self.tmp / "data/plain/ok.mp4", b"2")
        real = importer._is_link
        with mock.patch.object(importer, "_is_link", lambda path: path.name == "junction" or real(path)):
            assets, issues = run([self.tmp / "data"])
        self.assertEqual([a["relative_path"] for a in assets], ["plain/ok.mp4"])
        self.assertEqual(codes(issues), ["symlink_skipped"])
        self.assertTrue(issues[0]["path"].endswith("junction"))

    def test_names_sort_without_regard_to_case(self):
        for name in ("B.mp4", "a.mp4", "C.mp4", "d.mp4"):
            write(self.tmp / name, name.encode())
        assets, _ = run([self.tmp])
        self.assertEqual([a["relative_path"] for a in assets], ["a.mp4", "B.mp4", "C.mp4", "d.mp4"])

    def test_a_same_size_rewrite_is_detected_by_the_timestamp(self):
        path = write(self.tmp / "a.mp4", b"x" * 1000)
        real = hashlib.file_digest

        def rewrite(stream, name):
            digest = real(stream, name)
            path.write_bytes(b"y" * 1000)
            later = path.stat().st_mtime_ns + 10**9
            os.utime(path, ns=(later, later))
            return digest

        with mock.patch.object(importer.hashlib, "file_digest", rewrite):
            with self.assertRaisesRegex(ValueError, "changed"):
                importer.fingerprint_file(path, "a.mp4")

    def test_growth_is_detected_even_if_the_timestamp_is_restored(self):
        path = write(self.tmp / "a.mp4", b"x" * 1000)
        original = path.stat().st_mtime_ns
        real = hashlib.file_digest

        def grow(stream, name):
            digest = real(stream, name)
            with open(path, "ab") as other:
                other.write(b"more")
            os.utime(path, ns=(original, original))
            return digest

        with mock.patch.object(importer.hashlib, "file_digest", grow):
            with self.assertRaisesRegex(ValueError, "changed"):
                importer.fingerprint_file(path, "a.mp4")


class LinkTests(TempDirCase):
    def link(self, target, path, directory=False):
        try:
            os.symlink(target, path, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        return path

    def test_links_inside_a_searched_folder_are_skipped_and_reported(self):
        write(self.tmp / "data/real.mp4", b"1")
        write(self.tmp / "elsewhere/secret.mp4", b"2")
        data = self.tmp / "data"
        self.link(self.tmp / "elsewhere", data / "dir_link", directory=True)
        self.link(self.tmp / "elsewhere/secret.mp4", data / "file_link.mp4")
        self.link(self.tmp / "gone.mp4", data / "broken.mp4")
        assets, issues = run([data])
        self.assertEqual([a["relative_path"] for a in assets], ["real.mp4"])
        self.assertEqual(sorted(codes(issues)), ["symlink_skipped"] * 3)

    def test_skipped_links_are_reported_in_name_order_whatever_the_listing_order(self):
        write(self.tmp / "elsewhere/x.mp4", b"1")
        data = self.tmp / "data"
        data.mkdir()
        for letter in "hgfedcba":  # created in reverse so creation order cannot be mistaken for name order
            self.link(self.tmp / "elsewhere", data / f"{letter}_dir", directory=True)
            self.link(self.tmp / "elsewhere/x.mp4", data / f"{letter}_file.mp4")
        _, issues = run([data])
        names = [Path(i["path"]).name for i in issues if i["code"] == "symlink_skipped"]
        self.assertEqual(names, [f"{c}_dir" for c in "abcdefgh"] + [f"{c}_file.mp4" for c in "abcdefgh"])

    def test_reaching_data_through_a_linked_path_works(self):
        write(self.tmp / "real/a.mp4", b"1")
        link = self.link(self.tmp / "real", self.tmp / "linked", directory=True)
        assets, issues = run([link])
        self.assertEqual([a["relative_path"] for a in assets], ["a.mp4"])
        self.assertEqual(issues, [])
        self.assertEqual(assets[0]["path"], str((self.tmp / "real/a.mp4").resolve()))

    def test_explicit_link_to_a_file_is_followed(self):
        write(self.tmp / "real.mp4", b"1")
        link = self.link(self.tmp / "real.mp4", self.tmp / "alias.mp4")
        assets, _ = run([link])
        self.assertEqual([a["relative_path"] for a in assets], ["real.mp4"])


if __name__ == "__main__":
    unittest.main()
