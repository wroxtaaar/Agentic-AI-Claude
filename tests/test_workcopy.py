import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from vpsagents import gitops, workcopy
from vpsagents.workcopy import UnsafeCopy

from .helpers import GIT_ENV, make_repo, sh


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = make_repo(self.root, "proj")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("print('a')\n")
        (self.repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(self.repo / "run.sh", 0o755)
        (self.repo / ".gitignore").write_text("*.log\nbuild/\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "more")
        self.wt = self.root / "wt"
        sh(self.repo, "git", "worktree", "add", "-q", "-b", "job", str(self.wt))
        self.scr_root = self.root / "scratch"
        self.s = workcopy.create(self.wt, self.scr_root)

    def tearDown(self):
        self.s.cleanup()
        self._td.cleanup()


class CopyTests(Base):
    def test_copy_contains_tracked_files_and_its_own_standalone_git_repo(self):
        p = self.s.path
        self.assertEqual((p / "x.txt").read_text(), "old\n")
        self.assertTrue((p / ".git").is_dir())  # a real directory, not a pointer outside the copy
        self.assertEqual(sh(p, "git", "log", "--oneline").count("\n"), 0)
        self.assertEqual(sh(p, "git", "status", "--porcelain"), "")
        self.assertTrue(os.access(p / "run.sh", os.X_OK))

    def test_untracked_and_ignored_files_are_not_copied(self):
        (self.wt / "untracked.txt").write_text("u")
        (self.wt / "debug.log").write_text("l")
        s2 = workcopy.create(self.wt, self.scr_root)
        try:
            self.assertFalse((s2.path / "untracked.txt").exists())
            self.assertFalse((s2.path / "debug.log").exists())
        finally:
            s2.cleanup()

    def test_the_real_dot_git_pointer_is_never_in_the_copy(self):
        self.assertTrue((self.wt / ".git").is_file())  # the worktree has the pointer file
        self.assertFalse(any("worktrees" in line for line in (self.s.path / ".git" / "config").read_text().splitlines()))
        self.assertNotIn(str(self.repo), (self.s.path / ".git" / "config").read_text())

    def test_cleanup_handles_directories_with_no_permissions(self):
        d = self.s.path / "locked"
        d.mkdir()
        (d / "f").write_text("x")
        os.chmod(d, 0)
        self.s.cleanup()
        self.assertFalse(self.s.path.exists())

    def test_tracked_symlink_is_copied_as_a_symlink(self):
        os.symlink("x.txt", self.wt / "alias")
        sh(self.wt, "git", "add", "alias")
        sh(self.wt, "git", "commit", "-qm", "link")
        s2 = workcopy.create(self.wt, self.scr_root)
        try:
            self.assertTrue((s2.path / "alias").is_symlink())
            self.assertEqual(workcopy.import_changes(s2, self.wt).changed, False)  # unchanged symlink is fine
        finally:
            s2.cleanup()


class ImportTests(Base):
    def test_no_changes_means_nothing_to_import(self):
        self.assertFalse(workcopy.import_changes(self.s, self.wt).changed)

    def test_modify_add_delete_roundtrip(self):
        p = self.s.path
        (p / "x.txt").write_text("new\n")
        (p / "src" / "b.py").write_text("print('b')\n")
        (p / "docs").mkdir()
        (p / "docs" / "n.md").write_text("# n\n")
        (p / "src" / "a.py").unlink()
        rep = workcopy.import_changes(self.s, self.wt)
        self.assertEqual(sorted(rep.modified), ["x.txt"])
        self.assertEqual(sorted(rep.added), ["docs/n.md", "src/b.py"])
        self.assertEqual(rep.deleted, ["src/a.py"])
        self.assertEqual((self.wt / "x.txt").read_text(), "new\n")
        self.assertEqual((self.wt / "docs" / "n.md").read_text(), "# n\n")
        self.assertFalse((self.wt / "src" / "a.py").exists())
        # and the control plane's normal commit step then sees exactly those changes
        head = gitops.commit_changes(gitops.Worktree(self.wt, "job", "x"), "m")
        self.assertTrue(head)

    def test_exec_bit_is_preserved_and_set(self):
        p = self.s.path
        os.chmod(p / "x.txt", 0o755)
        (p / "tool.sh").write_text("#!/bin/sh\n")
        os.chmod(p / "tool.sh", 0o700)
        workcopy.import_changes(self.s, self.wt)
        self.assertTrue(os.stat(self.wt / "x.txt").st_mode & 0o100)
        self.assertEqual(stat.S_IMODE(os.stat(self.wt / "tool.sh").st_mode), 0o755)

    def test_emptied_directory_is_removed(self):
        (self.s.path / "src" / "a.py").unlink()
        os.rmdir(self.s.path / "src")
        workcopy.import_changes(self.s, self.wt)
        self.assertFalse((self.wt / "src").exists())

    def test_ignored_and_tool_junk_is_dropped_and_reported(self):
        p = self.s.path
        (p / "x.txt").write_text("new\n")
        (p / "out.log").write_text("noise")
        (p / "build").mkdir()
        (p / "build" / "o.bin").write_text("o")
        (p / ".aider.chat.history.md").write_text("chat")
        (p / "src" / "__pycache__").mkdir()
        (p / "src" / "__pycache__" / "a.pyc").write_text("c")
        rep = workcopy.import_changes(self.s, self.wt)
        self.assertEqual(rep.added, [])
        self.assertEqual(sorted(rep.ignored), [".aider.chat.history.md", "build/o.bin", "out.log", "src/__pycache__/a.pyc"])
        self.assertFalse((self.wt / "out.log").exists())
        self.assertFalse((self.wt / ".aider.chat.history.md").exists())


class RejectionTests(Base):
    def assertRejected(self, text):
        with self.assertRaises(UnsafeCopy) as cm:
            workcopy.import_changes(self.s, self.wt)
        self.assertIn(text, str(cm.exception))
        # nothing at all was written to the real worktree
        self.assertEqual(sh(self.wt, "git", "status", "--porcelain"), "")

    def test_new_symlink(self):
        (self.s.path / "x.txt").write_text("new\n")  # a legit change alongside must not sneak through either
        os.symlink("/etc/passwd", self.s.path / "link")
        self.assertRejected("symlink")
        self.assertEqual((self.wt / "x.txt").read_text(), "old\n")

    def test_file_replaced_by_symlink(self):
        os.unlink(self.s.path / "x.txt")
        os.symlink("/etc/passwd", self.s.path / "x.txt")
        self.assertRejected("symlink")

    def test_directory_replaced_by_symlink_does_not_follow_it(self):
        import shutil
        shutil.rmtree(self.s.path / "src")
        os.symlink("/tmp", self.s.path / "src")
        self.assertRejected("symlink")

    def test_special_file(self):
        os.mkfifo(self.s.path / "pipe")
        self.assertRejected("special file")

    def test_nested_dot_git_directory_or_file(self):
        (self.s.path / "src" / ".git").mkdir()
        self.assertRejected("nested .git")

    def test_sensitive_paths(self):
        for name in (".env", "id_rsa", "deploy.pem", ".env.production"):
            with self.subTest(name):
                (self.s.path / name).write_text("S=1")
                self.assertRejected("sensitive")
                os.unlink(self.s.path / name)

    def test_secret_dir_part(self):
        (self.s.path / ".ssh").mkdir()
        (self.s.path / ".ssh" / "config").write_text("Host x")
        self.assertRejected("sensitive")

    def test_too_many_files(self):
        for i in range(gitops.MAX_FILES + 1):
            (self.s.path / f"f{i}.txt").write_text("x")
        self.assertRejected("too many changed files")

    def test_file_too_large(self):
        (self.s.path / "big.bin").write_bytes(b"x" * (workcopy.MAX_FILE_BYTES + 1))
        self.assertRejected("too large")

    def test_total_size_too_large(self):
        for i in range(3):
            (self.s.path / f"b{i}.bin").write_bytes(b"x" * 900_000)
        self.assertRejected("changes too large")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions; this case only exists for normal users")
    def test_directory_without_permissions_fails_closed(self):
        d = self.s.path / "hidden"
        d.mkdir()
        (d / "secret-change").write_text("x")
        os.chmod(d, 0)
        try:
            self.assertRejected("unreadable")
        finally:
            os.chmod(d, 0o700)

    def test_tampered_scratch_git_dir_is_ignored_not_executed(self):
        # a hostile config in the copy's own .git must never matter: we never run git there after the run
        marker = self.root / "PWNED"
        (self.s.path / ".git" / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
        (self.s.path / "x.txt").write_text("new\n")
        workcopy.import_changes(self.s, self.wt)
        self.assertFalse(marker.exists())

    def test_removed_dot_git_in_scratch_is_harmless(self):
        import shutil
        shutil.rmtree(self.s.path / ".git")
        (self.s.path / "x.txt").write_text("new\n")
        self.assertEqual(workcopy.import_changes(self.s, self.wt).modified, ["x.txt"])


class RealWorktreeGuards(Base):
    def test_refuses_to_write_through_a_symlinked_directory_in_the_worktree(self):
        outside = self.root / "outside"
        outside.mkdir()
        # the worktree has a directory symlink at "linked" (tracked)
        os.symlink(str(outside), self.wt / "linked")
        sh(self.wt, "git", "add", "linked")
        sh(self.wt, "git", "commit", "-qm", "dirlink")
        s2 = workcopy.create(self.wt, self.scr_root)
        try:
            os.unlink(s2.path / "linked")  # model replaces the symlink with a real directory containing a file
            (s2.path / "linked").mkdir()
            (s2.path / "linked" / "evil.txt").write_text("x")
            with self.assertRaises(UnsafeCopy):
                workcopy.import_changes(s2, self.wt)
            self.assertFalse((outside / "evil.txt").exists())
        finally:
            s2.cleanup()

    def test_file_replacing_a_directory_is_refused(self):
        import shutil
        shutil.rmtree(self.s.path / "src")
        (self.s.path / "src").write_text("now a file")
        # src/a.py deleted + src added as a file: valid change, applied in the right order
        rep = workcopy.import_changes(self.s, self.wt)
        self.assertEqual(sorted(rep.added), ["src"])
        self.assertTrue((self.wt / "src").is_file())

    def test_directory_with_other_content_is_not_replaced_by_a_file(self):
        import shutil
        (self.wt / "src" / "keep.txt").write_text("untracked local content")  # not in the copy, so not deleted by the change
        shutil.rmtree(self.s.path / "src")
        (self.s.path / "src").write_text("now a file")
        with self.assertRaises(UnsafeCopy) as cm:
            workcopy.import_changes(self.s, self.wt)
        self.assertIn("other content", str(cm.exception))
        self.assertTrue((self.wt / "src" / "keep.txt").exists())

    def test_no_stray_temp_files_left_behind(self):
        (self.s.path / "x.txt").write_text("new\n")
        workcopy.import_changes(self.s, self.wt)
        self.assertEqual([p.name for p in self.wt.iterdir() if p.name.startswith(".vpsa-")], [])


if __name__ == "__main__":
    unittest.main()
