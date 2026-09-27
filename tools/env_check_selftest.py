r"""The dependency pin verifies; it does not agree with whatever it finds.

    .venv\\Scripts\\python tools\\env_check_selftest.py

`tools\\vendor_lock.ps1` used to end by writing `vendor/LOCK.txt` from whatever was on
disk, with `-Ref` defaulting to `main`. So the install path was: download a moving
branch, hash it, and call the result the known-good set — after which `-Verify` compared
that tree against that lock and reported peace. A verifier that is also the thing being
verified cannot fail, and that is the bug every case here is aimed at.

The fixtures are real files in a temporary directory, so the digests, the text/binary
rule and the "present but not in the lock" walk are all exercised as they will be on a
desk. Two cases look at this repository itself — the committed lock and the manifest —
because those are artefacts that can regress without any code changing.

Nothing here needs the vendored library, the fonts, psutil, or a network.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import env_check as ec     # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []

GOOD = "2b33ab4f00a096916dd6a1174441a53a7ec33b03"
OTHER = "0000000000000000000000000000000000000001"
DLL = "external/LibreHardwareMonitor/LibreHardwareMonitorLib.dll"
# A pointer, not a binary: this is what large-file storage or an aborted blob fetch
# leaves where a 1.2 MB DLL should be, and `os.path.exists` says yes to it.
LFS_POINTER = (b"version https://git-lfs.github.com/spec/v1\n"
               b"oid sha256:689b000000000000000000000000000000000000000000000000000000000000\n"
               b"size 1203200\n")


def sha256_of(path: Path) -> str:
    """A short fingerprint of a file's exact bytes, for "did this change?"."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def fails_with(rows, needle: str) -> bool:
    """Did something fail, and did it say the true reason?"""
    return any(lv == ec.FAIL and needle in text for lv, text in rows)


class Fixture:
    """A checkout with a vendored tree and a lock that matches it."""

    def __init__(self, root: Path, commit: str = GOOD) -> None:
        self.root = root
        self.tree = root / ec.VENDOR
        (root / "vendor").mkdir(parents=True, exist_ok=True)
        self.tree.mkdir(parents=True, exist_ok=True)
        self.write("library/lcd/lcd_comm.py", b"ORIENTATION = 'up'\n")
        self.write("library/lcd/serialize.py", b"def pack():\n    return 1\n")
        # A DLL with a NUL in it, because the text rule must not touch it.
        self.write(DLL, b"MZ\x90\x00\x03\x00\r\n\x00\x00binary body\r\n")
        for f in ec.FONTS:
            self.write(f, b"\x00\x01\x00\x00fake font face")
        self.relock(commit)

    def write(self, rel: str, data: bytes) -> Path:
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def drop(self, rel: str) -> None:
        (self.tree / rel).unlink()

    def relock(self, commit: str = GOOD) -> None:
        entries = {rel: ec.digest(p) for rel, p in ec.collect(self.tree).items()}
        lines = ec.lock_lines(commit, entries)
        (self.root / ec.LOCK).write_text("\n".join(lines) + "\n", encoding="utf-8",
                                         newline="\n")

    def retarget_lock(self, commit_line: str) -> None:
        """Rewrite just the pin line, leaving every hash exactly as it was."""
        path = self.root / ec.LOCK
        lines = [commit_line if l.lstrip().startswith("# commit:") else l
                 for l in path.read_text(encoding="utf-8").splitlines()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def vendor_listing(self) -> list:
        return sorted(p.relative_to(self.root / "vendor").as_posix()
                      for p in (self.root / "vendor").rglob("*"))

    def problems(self):
        return ec.check_vendor(self.root)


def run(name: str, fn) -> None:
    """A case that raises is a failed case, not a crashed run."""
    print(f"case: {name}")
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="envcheck-"))
        fn(tmp)
    except Exception as e:  # noqa: BLE001 - a crash is a finding, not a clean exit
        fails.append(name)
        print(f"  FAIL {name}: raised {type(e).__name__}: {e}")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        print()


# --------------------------------------------------------------------------- cases
def case_complete_tree_verifies(tmp: Path) -> None:
    fx = Fixture(tmp)
    check("a tree that matches the lock reports no problem", fx.problems(),
          [(ec.OK, f"vendored tree matches {ec.LOCK} at {GOOD} (6 files)")])
    check("and the DLL and fonts are believed present",
          [lv for lv, _ in ec.check_dll(tmp, True) + ec.check_fonts(tmp, True)],
          [ec.OK, ec.OK])


def case_drift_is_caught_and_the_lock_is_untouched(tmp: Path) -> None:
    fx = Fixture(tmp)
    lock_before = sha256_of(tmp / ec.LOCK)
    listing_before = fx.vendor_listing()
    fx.write("library/lcd/lcd_comm.py", b"ORIENTATION = 'down'\n")   # one byte moved
    rows = fx.problems()
    check("one changed byte is drift", fails_with(rows, f"drift from {ec.LOCK}: "
                                                  "library/lcd/lcd_comm.py"), True)
    check("checking did not rewrite the lock", sha256_of(tmp / ec.LOCK), lock_before)
    check("checking created nothing", fx.vendor_listing() == listing_before, True)


def case_branch_pin_is_refused(tmp: Path) -> None:
    fx = Fixture(tmp)
    fx.retarget_lock("# commit:   main")
    check("a branch is not accepted as a pin",
          fails_with(fx.problems(), "not a 40-character commit SHA"), True)
    fx.retarget_lock("# commit:   2b33ab4")     # an abbreviation is not a pin either
    check("nor is an abbreviated SHA", fails_with(fx.problems(), "not a 40-character"),
          True)
    fx.retarget_lock("# this lock names no revision at all")
    check("and a lock with no revision line says so",
          fails_with(fx.problems(), "names no upstream commit"), True)


def case_line_endings_are_not_identity(tmp: Path) -> None:
    fx = Fixture(tmp)
    fx.write("library/lcd/lcd_comm.py", b"ORIENTATION = 'up'\r\n")   # CRLF checkout
    check("the same text with CRLF still verifies", fx.problems(),
          [(ec.OK, f"vendored tree matches {ec.LOCK} at {GOOD} (6 files)")])
    fx.write(DLL, b"MZ\x90\x00\x03\x00\x00\x00binary body\n")  # CRLF folded away
    check("but a binary file's bytes are its identity",
          fails_with(fx.problems(), f"drift from {ec.LOCK}: {DLL}"), True)


def case_unnamed_file_is_reported(tmp: Path) -> None:
    fx = Fixture(tmp)
    fx.write("library/lcd/sneaky.py", b"import os\n")
    check("a file under a scoped directory that the lock does not name is a problem",
          fails_with(fx.problems(), f"present but not in {ec.LOCK}: library/lcd/sneaky.py"),
          True)
    fx.write("res/fonts/roboto/Roboto-Light.ttf", b"\x00\x01unused face")
    check("a font we never select is not",
          any(lv == ec.FAIL and "Roboto-Light" in t for lv, t in fx.problems()), False)


def case_partial_fetch_is_named(tmp: Path) -> None:
    fx = Fixture(tmp)
    fx.drop(DLL)
    rows = fx.problems()
    check("a missing DLL is reported by the lock walk",
          fails_with(rows, f"missing from the vendored tree: {DLL}"), True)
    check("and again by the check the LHM backend would make",
          fails_with(ec.check_dll(tmp, True), "absent"), True)
    check("a partial tree is a failure even when the app could fall back",
          [lv for lv, _ in ec.check_dll(tmp, False)], [ec.WARN])


def case_pointer_file_is_caught(tmp: Path) -> None:
    fx = Fixture(tmp)
    fx.write(DLL, LFS_POINTER)
    rows = fx.problems()
    check("a pointer file no longer matches the lock",
          fails_with(rows, f"drift from {ec.LOCK}: {DLL}"), True)
    check("and is named for what it is rather than failing in pythonnet",
          fails_with(ec.check_dll(tmp, True), "not a PE image"), True)


def silence(fn, *args):
    """Run something that prints, and keep what it printed out of the gate log.

    `write_lock` reports to stdout the way a command line does. Left loose inside a
    case, a line starting `FAIL` from a tool that was *asked* to refuse reads exactly
    like a case that failed - and the offline gate prints whatever a case emits.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = fn(*args)
    return got, buf.getvalue()


def case_write_lock_refuses_a_branch(tmp: Path) -> None:
    fx = Fixture(tmp)
    before = sha256_of(tmp / ec.LOCK)
    code, said = silence(ec.write_lock, tmp, fx.tree, "main")
    check("--write-lock refuses to record a branch", code, 1)
    check("and says which rule it ran into",
          "40-character" in said and "main" in said, True)
    check("and leaves the recorded lock alone", sha256_of(tmp / ec.LOCK), before)
    check("--write-lock accepts a commit",
          silence(ec.write_lock, tmp, fx.tree, OTHER)[0], 0)
    commit, entries = ec.parse_lock(tmp / ec.LOCK)
    check("recording it re-reads as the same pin and files",
          (commit, len(entries)), (OTHER, 6))


def case_manifest_is_checked(tmp: Path) -> None:
    (tmp / "app").mkdir()
    (tmp / "tools").mkdir()
    (tmp / "app" / "thing.py").write_text("import psutil\nimport widgetlib\n",
                                          encoding="utf-8")
    (tmp / "requirements.txt").write_text("psutil>=7.0\n", encoding="utf-8")
    rows = ec.check_manifest(tmp)
    check("an import nobody pinned is a failure",
          fails_with(rows, "widgetlib (pip: widgetlib)"), True)
    check("and a floating version is a failure too",
          fails_with(rows, "not pinned with `==`"), True)
    (tmp / "requirements.txt").write_text("psutil==7.2.2\nwidgetlib==1.0\n",
                                          encoding="utf-8")
    check("both go away once the manifest says what is installed",
          [lv for lv, _ in ec.check_manifest(tmp)], [ec.WARN])   # no -lhm file yet


def case_the_committed_lock_is_a_real_pin(tmp: Path) -> None:
    """The artefact, not the code that reads it."""
    commit, entries = ec.parse_lock(ROOT / ec.LOCK)
    check("vendor/LOCK.txt pins an immutable revision", ec.pin_problem(commit), None)
    check("it names the library, the fonts and the DLL it claims to",
          len(entries) >= 50, True)
    check("including the DLL the LHM backend loads", DLL in entries, True)
    check("and every theme font the layout selects",
          [f in entries for f in ec.FONTS], [True, True, True])
    outside = [e for e in entries
               if not any(e.startswith(s["dir"] + "/") for s in ec.SCOPES)
               and e not in ec.FONTS]
    check("nothing is locked outside the scope the fetch takes", outside, [])
    unscoped = [s["sparse"] for s in ec.SCOPES
                if not any(e.startswith(s["dir"] + "/") for e in entries)]
    check("and nothing in that scope is left unlocked", unscoped, [])


def case_the_manifest_covers_the_real_tree(tmp: Path) -> None:
    rows = ec.check_manifest(ROOT)
    check("requirements.txt covers every third-party import in app/ and tools/",
          [t for lv, t in rows if lv == ec.FAIL], [])
    pins = ec.read_requirements(ROOT / "requirements.txt")
    check("and pins the seven the offline gate installs", len(pins), 7)
    lhm = ec.read_requirements(ROOT / "requirements-lhm.txt")
    check("the LHM extra adds pythonnet and inherits the rest",
          ("pythonnet" in lhm), True)


def main() -> int:
    for name, fn in (
        ("a complete tree verifies", case_complete_tree_verifies),
        ("one changed byte is drift, and checking cannot bless it",
         case_drift_is_caught_and_the_lock_is_untouched),
        ("a pin that names a branch is refused", case_branch_pin_is_refused),
        ("line endings are not dependency identity", case_line_endings_are_not_identity),
        ("a file the lock does not name is reported", case_unnamed_file_is_reported),
        ("a partial fetch is named, not shrugged at", case_partial_fetch_is_named),
        ("a pointer file where a DLL belongs is caught", case_pointer_file_is_caught),
        ("re-recording the lock refuses a branch", case_write_lock_refuses_a_branch),
        ("the manifest check bites", case_manifest_is_checked),
        ("the committed lock is a real pin", case_the_committed_lock_is_a_real_pin),
        ("the manifest covers this tree", case_the_manifest_covers_the_real_tree),
    ):
        run(name, fn)
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())