r"""Is what is installed what this repository reviewed?

    .venv\\Scripts\\python tools\\env_check.py             # report on this checkout
    .venv\\Scripts\\python tools\\env_check.py --strict    # the same, for install_autostart
    .venv\\Scripts\\python tools\\env_check.py --manifest    # also check requirements.txt
    .venv\\Scripts\\python tools\\env_check.py --print-scope   # what the fetch takes

    (its own regression cases are tools\\env_check_selftest.py)

This is the one place that answers "is the dependency set right", and it only ever
*reads*. That last part is the point. The install script used to answer the same
question by writing a new `vendor/LOCK.txt` from whatever it had just downloaded, so
`-Fetch` from a moving branch produced a lock that agreed with the branch it had just
moved to, and the `-Verify` that followed was a formality: nothing was being compared
against anything reviewed. A verifier that can also bless is not a verifier.

So the pin lives in `vendor/LOCK.txt` — an immutable upstream commit plus a sha256 for
every file we import — and this script checks the tree against it. `tools\\vendor_lock.ps1
-Update` is the only thing allowed to write that file, and using it is a decision, not
a step in an install.

Nothing here imports psutil, PIL, yaml or anything else it is about to check for: a
validator that dies because a dependency is missing cannot tell you which one.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = "vendor/turing-smart-screen-python"
LOCK = "vendor/LOCK.txt"

# The floor is not a preference: several modules (`app/config.py` among them) write
# `str | None` without `from __future__ import annotations`, so the union is evaluated
# at runtime and older interpreters fail at import. CI and the desk run 3.12.
MIN_PYTHON = (3, 10)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# What the fetch takes, and therefore what the lock is complete about. `dir` is a path
# under the vendored tree; `suffix` narrows it to one kind of file, and `None` means
# every file in that directory. `sparse` is what `git sparse-checkout set` is told —
# one definition, read by both the Python checker and the PowerShell fetch, because a
# fetch that takes one more directory than the lock knows about looks like a clean
# install right up until the file it did not lock is loaded.
SCOPES = (
    {"dir": "library", "suffix": ".py", "sparse": "library"},
    {"dir": "external/LibreHardwareMonitor", "suffix": None,
     "sparse": "external/LibreHardwareMonitor"},
)
# Named one by one, not by directory: upstream's `res/fonts` is full of faces and CJK
# subsets this layout never selects, and a lock that covered them would be a lock
# nobody can read.
FONTS = (
    "res/fonts/jetbrains-mono/JetBrainsMono-ExtraBold.ttf",
    "res/fonts/roboto/Roboto-Bold.ttf",
    "res/fonts/roboto/Roboto-Medium.ttf",
)

# import name -> pip name. The left column is what the source says, the right is what
# `pip install` is called; they disagree often enough that the mapping is the useful
# part (PIL is pillow, serial is pyserial, pynvml is nvidia-ml-py).
MODULES = {
    "PIL": "pillow",
    "numpy": "numpy",
    "psutil": "psutil",
    "pynvml": "nvidia-ml-py",
    "serial": "pyserial",
    "usb": "pyusb",
    "yaml": "PyYAML",
}
# The LibreHardwareMonitor backend's import. Optional for the app (it falls back to
# psutil), not optional for the elevated autostart, whose whole reason for existing is
# the ring0 access that backend uses.
LHM_MODULE = ("clr", "pythonnet")

OK, WARN, FAIL = "ok", "WARN", "FAIL"


class LockError(Exception):
    """The lock file is missing or malformed — which is a finding, not a crash."""


# ------------------------------------------------------------------------- the lock
def parse_lock(path: Path):
    """Read a vendor LOCK.txt into `(commit, {relpath: sha256})`.

    A lock that cannot be parsed raises rather than returning a partial answer: half a
    lock verifies half a tree, and a silent half is how a dependency ends up unwatched.
    """
    if not path.is_file():
        raise LockError(f"no lock file at {path}")
    commit, entries = None, {}
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            m = re.match(r"^#\s*commit\s*:\s*(\S+)", s)
            if m:
                commit = m.group(1)
            continue
        parts = s.split()
        if len(parts) != 2 or not SHA256_RE.match(parts[0]):
            raise LockError(f"{path.name} line {n}: expected '<sha256>  <path>', "
                            f"got {line!r}")
        entries[parts[1].replace("\\", "/")] = parts[0]
    return commit, entries


def pin_problem(commit) -> str | None:
    """Why this pin is not an immutable revision, or None if it is one."""
    if commit is None:
        return (f"{LOCK} names no upstream commit: add `# commit: <40 hex>` so an "
                "install can say which revision it is reproducing")
    if not COMMIT_RE.match(commit):
        return (f"{LOCK} pins {commit!r}, which is not a 40-character commit SHA. A "
                "branch is a moving target: installing from one gets whatever upstream "
                "heads to next, and the lock then describes that instead of what was "
                "reviewed")
    return None


def digest(path: Path) -> str:
    """sha256 of a file's content, with line endings factored out for text.

    Text is hashed with CRLF folded to LF, which is the same rule this repository's
    own `.gitattributes` applies (`* text=auto`), and the binary test is git's: a NUL
    anywhere in the first 8 KB. It is in the lock and not an accident. Without it the
    hash depends on the *checker's* `core.autocrlf` rather than on the bytes we import,
    and that dependency is precisely why the old `-Fetch` had to rewrite the lock: a
    fresh checkout writes the `.py` files with LF, the tracked copy on a Windows desk
    has CRLF, and every hash mismatched even though nothing had changed. A binary file
    keeps its exact bytes, so a DLL whose content differs by one byte still fails.
    """
    raw = path.read_bytes()
    if b"\0" not in raw[:8000]:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def lock_lines(commit: str, entries: dict) -> list:
    """The lock file's text, in the shape `parse_lock` reads back."""
    head = [
        "# sha256 of the vendored turing-smart-screen-python files we import",
        "# upstream: https://github.com/mathoudebine/turing-smart-screen-python",
        f"# commit:   {commit}",
        "# check: tools\\vendor_lock.ps1 -Verify      re-pin (maintainers): "
        "tools\\vendor_lock.ps1 -Update -Sha <40 hex>",
        "# Hashes are of content with CRLF folded to LF in text files (see "
        "tools/env_check.py), so they do not depend on git's core.autocrlf.",
    ]
    return head + [f"{entries[p]}  {p}" for p in sorted(entries)]


# ------------------------------------------------------------------ collecting files
def collect(tree: Path) -> dict:
    """Every file the lock is expected to name, as {relpath: Path}."""
    out = {}
    for scope in SCOPES:
        base = tree / scope["dir"]
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*") if scope["suffix"] is None else
                        base.rglob(f"*{scope['suffix']}")):
            if p.is_file():
                out[p.relative_to(tree).as_posix()] = p
    for rel in FONTS:
        p = tree / rel
        if p.is_file():
            out[rel] = p
    return out


def on_disk_in_scope(tree: Path) -> set:
    """Files under a scoped directory, whether or not the lock knows them.

    This is what turns "someone fetched a bigger tree than the lock describes" into a
    message. Fonts are deliberately not covered: they are named one by one, and the
    rest of upstream's font directory is not ours to police.
    """
    seen = set()
    for scope in SCOPES:
        base = tree / scope["dir"]
        if not base.is_dir():
            continue
        paths = (base.rglob("*") if scope["suffix"] is None
                 else base.rglob(f"*{scope['suffix']}"))
        for p in paths:
            if p.is_file():
                seen.add(p.relative_to(tree).as_posix())
    return seen


# ------------------------------------------------------------------- the actual checks
def check_vendor(root: Path, tree: Path | None = None, lock: Path | None = None) -> list:
    """Every way the installed vendored tree differs from the reviewed one.

    `tree` and `lock` are overridable because the fetch verifies a staged checkout
    against a *candidate* lock before it replaces either the working copy or the
    committed lock, and a failure there has to leave both exactly as they were.
    """
    try:
        commit, want = parse_lock(lock or root / LOCK)
    except LockError as e:
        return [(FAIL, f"vendor lock unreadable: {e}")]
    out = []
    bad = pin_problem(commit)
    if bad:
        out.append((FAIL, bad))
    if not want:
        out.append((FAIL, f"{LOCK} names no files"))
    tree = tree or root / VENDOR
    if not tree.is_dir():
        out.append((FAIL, f"vendored library absent: {tree}"))
        out.append((FAIL, "       run: powershell -File tools\\vendor_lock.ps1 -Fetch"))
        return out
    for rel in sorted(want):
        f = tree / rel
        if not f.is_file():
            out.append((FAIL, f"missing from the vendored tree: {rel}"))
        elif digest(f) != want[rel]:
            out.append((FAIL, f"drift from {LOCK}: {rel}"))
    for rel in sorted(on_disk_in_scope(tree) - set(want)):
        out.append((FAIL, f"present but not in {LOCK}: {rel}"))
    if not out:
        out.append((OK, f"vendored tree matches {LOCK} at {commit} ({len(want)} files)"))
    return out


def check_python() -> list:
    v = sys.version_info[:3]
    if sys.version_info[:2] < MIN_PYTHON:
        return [(FAIL, f"Python {'.'.join(map(str, v))} is older than the "
                       f"{'.'.join(map(str, MIN_PYTHON))} this app needs "
                       "(PEP 604 unions are evaluated at runtime)")]
    return [(OK, f"Python {'.'.join(map(str, v))}")]


def _missing(names) -> list:
    import importlib.util
    return [n for n in names if importlib.util.find_spec(n) is None]


def check_modules(strict: bool) -> list:
    gone = _missing(sorted(MODULES))
    out = []
    if gone:
        pip = " ".join(MODULES[m] for m in gone)
        out.append((FAIL if strict else WARN,
                    f"missing modules: {', '.join(gone)} "
                    f"(pip install {pip}, or pip install -r requirements.txt)"))
    else:
        out.append((OK, f"{len(MODULES)} third-party modules import"))
    mod, pipname = LHM_MODULE
    if _missing([mod]):
        # The app answers this by falling back to psutil, so it is a warning at most —
        # except under --strict, where the caller is the elevated task whose purpose is
        # the ring0 readings this backend is the only source of.
        out.append((FAIL if strict else WARN,
                    f"{mod} ({pipname}) absent: the LibreHardwareMonitor backend is "
                    "unavailable, so CPU temperature and package power will read `--` "
                    "(pip install -r requirements-lhm.txt)"))
    else:
        out.append((OK, f"{mod} ({pipname}) imports"))
    return out


def check_dll(root: Path, strict: bool, tree: Path | None = None) -> list:
    """The one file `app/sensors/lhm.py` cannot work without, and what it really is."""
    dll = (tree or root / VENDOR) / "external/LibreHardwareMonitor/LibreHardwareMonitorLib.dll"
    if not dll.is_file():
        return [(FAIL if strict else WARN,
                 f"{dll.relative_to(root)} absent: the LHM backend cannot load")]
    with dll.open("rb") as fh:
        head = fh.read(2)
    if head != b"MZ":
        # A 133-byte text file where a 1.2 MB DLL should be is what a pointer for a
        # large-file-storage object, an aborted download, or a `--filter=blob:none`
        # checkout without the fetch leaves behind. `os.path.exists` says yes to all
        # three, and pythonnet then fails on a file that is not a PE image at all.
        return [(FAIL, f"{dll.relative_to(root)} is not a PE image (starts with "
                       f"{head!r}, not b'MZ'); it is {dll.stat().st_size} bytes of "
                       "something else - re-run tools\\vendor_lock.ps1 -Fetch")]
    return [(OK, f"LibreHardwareMonitorLib.dll ({dll.stat().st_size:,} bytes)")]


def check_presentmon(root: Path, strict: bool) -> list:
    exe = root / "vendor/presentmon/presentmon.exe"
    lock = root / "vendor/presentmon/LOCK.txt"
    if not exe.is_file():
        return [(FAIL if strict else WARN,
                 "vendor/presentmon/presentmon.exe absent: frame stats stay off "
                 "(powershell -File tools\\fetch_presentmon.ps1)")]
    if not lock.is_file():
        return [(FAIL, "vendor/presentmon/LOCK.txt absent: nothing says which build "
                       "this is, and `-Verify` has nothing to check against")]
    line = next((l.strip() for l in lock.read_text(encoding="utf-8-sig").splitlines()
                 if l.strip() and not l.strip().startswith("#")), None)
    if not line or not SHA256_RE.match(line.split()[0]):
        return [(FAIL, "vendor/presentmon/LOCK.txt has no sha256 line to verify")]
    if digest(exe) != line.split()[0]:
        return [(FAIL, "vendor/presentmon/presentmon.exe does not match its lock")]
    return [(OK, "presentmon.exe matches vendor/presentmon/LOCK.txt")]


def check_fonts(root: Path, strict: bool, tree: Path | None = None) -> list:
    gone = [f for f in FONTS if not ((tree or root / VENDOR) / f).is_file()]
    if gone:
        return [(FAIL if strict else WARN,
                 f"theme font(s) absent: {', '.join(Path(g).name for g in gone)} "
                 "- the layout falls back to a substitute face")]
    return [(OK, f"{len(FONTS)} theme fonts present")]


def check(root: Path, strict: bool = False) -> list:
    """The whole report, as (level, text) pairs."""
    return (check_python() + check_modules(strict) + check_vendor(root)
            + check_fonts(root, strict) + check_dll(root, strict)
            + check_presentmon(root, strict))


# ------------------------------------------------------------- the manifest is honest
def imported_names(root: Path) -> set:
    """Every third-party top-level module the app and its tools import."""
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    local = {"app", "tools", "main", "library", "presentmon_matrix", "usb_probe",
             "LibreHardwareMonitor"}     # the last three are vendored or CLR, not pip
    # A tool that imports its sibling is not a dependency. The project's own module
    # names come from the tree it is standing in, not from a hand-list that goes
    # stale the day someone adds a file — an invents-a-phantom-package manifest is
    # the same failure as an incomplete one, just noisier.
    for base in (root / "app", root / "tools"):
        local |= {q.stem for q in base.glob("*.py")}
    found = set()
    for base in (root / "app", root / "tools"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                # The filename so a real problem names its file, and the filter because
                # this is a name scanner and not a compiler: a legacy `'\S'` sitting in
                # some module's docstring would otherwise be reprinted by every check
                # that asks this question. Byte-compile owns syntax.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
            except (SyntaxError, UnicodeDecodeError):
                continue          # byte-compile catches that; this check is about names
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module.split(".")[0]]
                found |= {n for n in names if n and n not in stdlib and n not in local}
    return found


def read_requirements(path: Path) -> dict:
    """{pip name: specifier} from a requirements file, comments and `-r` lines out."""
    pins = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.split("#")[0].strip()
        if not s or s.startswith("-r") or s.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", s)
        if m:
            pins[m.group(1).lower()] = m.group(2).strip()
    return pins


def check_manifest(root: Path) -> list:
    """The manifest covers what the source imports, and pins it exactly.

    This is the check that keeps the manifest from rotting into a list someone once
    typed. An import added without a pin means a fresh checkout installs an environment
    that the developer's own venv has been quietly carrying, and the first person to
    clone finds out at install time.
    """
    out = []
    try:
        pins = read_requirements(root / "requirements.txt")
    except OSError as e:
        return [(FAIL, f"requirements.txt unreadable: {e}")]
    if not pins:
        return [(FAIL, "requirements.txt names no dependencies")]
    lhm = read_requirements(root / "requirements-lhm.txt") if \
        (root / "requirements-lhm.txt").is_file() else {}
    wanted = imported_names(root) - {LHM_MODULE[0]}
    uncovered = sorted(m for m in wanted if MODULES.get(m, m).lower() not in pins)
    if uncovered:
        out.append((FAIL, "imported but not in requirements.txt: "
                          ", ".join(f"{m} (pip: {MODULES.get(m, m)})" for m in uncovered)))
    loose = sorted(f"{k}{v}" for k, v in pins.items() if not v.startswith("=="))
    if loose:
        out.append((FAIL, "not pinned with `==`: " + ", ".join(loose)))
    if not lhm:
        out.append((WARN, "requirements-lhm.txt absent: nothing declares pythonnet"))
    if not out:
        out.append((OK, f"requirements.txt pins {len(pins)} distributions, and covers "
                        f"every third-party import in app/ and tools/"))
    return out


# ------------------------------------------------------------------------- reporting
def report(rows) -> int:
    for level, text in rows:
        print(f"{level:4s} {text}")
    bad = [t for lv, t in rows if lv == FAIL]
    print("\n" + ("ENVIRONMENT OK" if not bad
                  else f"ENVIRONMENT INCOMPLETE: {len(bad)} problem(s)"))
    return 1 if bad else 0


def write_lock(root: Path, tree: Path, sha: str, lock: Path | None = None) -> int:
    """The only thing in this repository that writes `vendor/LOCK.txt`.

    It is here rather than in PowerShell so that the file whose correctness the whole
    scheme rests on is produced by the same code that reads it back, and so that the
    one rule that matters can be enforced where it cannot be argued with: the lock
    records a commit, never a branch. `tools\\vendor_lock.ps1 -Update` is the only
    caller, and getting here is a decision someone made about a dependency, which is
    why it prints what moved instead of quietly replacing it.
    """
    if not COMMIT_RE.match(sha or ""):
        print(f"FAIL --sha must be a 40-character commit SHA, got {sha!r}. A branch "
              "name cannot go in a lock: by the time the next person installs, it "
              "points somewhere else and the lock describes that instead.")
        return 1
    files = collect(tree)
    if not files:
        print(f"FAIL nothing to lock under {tree} - is that checkout complete?")
        return 1
    entries = {rel: digest(p) for rel, p in files.items()}
    path = lock or root / LOCK
    try:
        old_commit, old = parse_lock(root / LOCK)
    except LockError:
        old_commit, old = None, {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lock_lines(sha, entries)) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"wrote {path} ({len(entries)} files)")
    if old_commit != sha:
        print(f"  commit: {old_commit} -> {sha}")
    added = sorted(set(entries) - set(old))
    removed = sorted(set(old) - set(entries))
    changed = sorted(k for k in set(old) & set(entries) if old[k] != entries[k])
    for label, names in (("added", added), ("removed", removed), ("changed", changed)):
        for n in names:
            print(f"  {label}: {n}")
    if (old_commit == sha and not added and not removed and not changed
            and old):
        print("  nothing moved: the tree already matches the recorded pin")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--tree", default=None,
                    help="check this vendored tree instead of vendor/turing-smart-screen-python "
                         "(the fetch uses it to verify a staged checkout before installing it)")
    ap.add_argument("--lock", default=None,
                    help="read/record the lock here instead of vendor/LOCK.txt "
                         "(the fetch verifies a candidate lock before installing it)")
    ap.add_argument("--strict", action="store_true",
                    help="treat an optional-for-the-app dependency as required "
                         "(what install_autostart.ps1 asks for)")
    ap.add_argument("--manifest", action="store_true",
                    help="also check requirements.txt against the imports")
    ap.add_argument("--only-vendor", action="store_true",
                    help="check the vendored tree, the fonts and the DLL, and nothing "
                         "to do with this interpreter")
    ap.add_argument("--write-lock", action="store_true",
                    help="re-record the lock from --tree at --sha (maintainers only)")
    ap.add_argument("--sha", default=None, help="the commit --write-lock is recording")
    ap.add_argument("--check-python", action="store_true",
                    help="report the running interpreter against the floor and exit "
                         "(tools/bootstrap.ps1 asks this of the python it found on PATH)")
    ap.add_argument("--print-scope", action="store_true",
                    help="print the git sparse-checkout paths and exit")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.check_python:
        return report(check_python())
    if a.print_scope:
        print(" ".join([s["sparse"] for s in SCOPES]
                       + ["res/fonts/jetbrains-mono", "res/fonts/roboto"]))
        return 0
    tree = Path(a.tree) if a.tree else None
    lock = Path(a.lock) if a.lock else None
    if a.write_lock:
        return write_lock(root, tree or root / VENDOR, a.sha, lock)
    if a.only_vendor:
        rows = (check_vendor(root, tree, lock) + check_fonts(root, True, tree)
                + check_dll(root, True, tree))
        return report(rows)
    rows = check(root, strict=a.strict)
    if a.manifest:
        rows += check_manifest(root)
    return report(rows)


if __name__ == "__main__":
    sys.exit(main())