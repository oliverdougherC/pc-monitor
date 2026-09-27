"""Steam identity against real process environments — no Steam required.

    .venv\\Scripts\\python tools\\steamid_selftest.py

`app/steamid.py` reads `SteamAppId`/`SteamGameId` out of `psutil.Process(pid).environ()`.
The trap is that psutil's Windows backend upper-cases every key it parses out of
the PEB environment block, so a fixture written the way the source used to spell
them (`steamappid`) proves nothing: it passes exactly where the real call fails.
The fixtures below are therefore the dictionaries Windows actually produces, and
one case asks a live child process what psutil really hands back.

Nothing here needs Steam, admin, or a game: identity is a pure read of an
environment mapping, and the mapping is the thing under test.
"""
import subprocess
import sys
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.steamid import steam_appid        # noqa: E402

sys.stdout.reconfigure(errors="replace")

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


def case_windows_uppercase_keys() -> None:
    print("case: the keys psutil's Windows backend actually returns")
    # {'STEAMAPPID': '730'} is what a Windows CS2 process reports. The lookup
    # this replaces spelled the names in lower case, so it found nothing here —
    # asserted below so the reason this file exists stays visible.
    env = {"SYSTEMROOT": "C:\\WINDOWS", "STEAMAPPID": "730", "USERNAME": "someone"}
    check("the old lower-case lookup finds nothing",
          next((env[k] for k in ("steamappid", "steamgameid")
                if env.get(k, "") not in ("", "0")), None), None)
    check("STEAMAPPID is read", steam_appid(env), "730")
    check("STEAMGAMEID alone is read", steam_appid({"STEAMGAMEID": "1234"}), "1234")


def case_mixed_case_and_spellings() -> None:
    print("case: whatever casing the platform handed over")
    for env, want in (
        ({"SteamAppId": "730"}, "730"),          # as Steam writes it (POSIX-ish)
        ({"steamappid": "730"}, "730"),          # as a test might write it
        ({"SteamAppId": "730", "steamgameid": "99"}, "730"),
        ({"STEAMAPPID": "730", "SteamGameId": "99"}, "730"),
    ):
        check(f"{sorted(env)} -> {want!r}", steam_appid(env), want)
    check("SteamAppId wins over SteamGameId",
          steam_appid({"STEAMGAMEID": "99", "STEAMAPPID": "730"}), "730")


def case_not_an_identity() -> None:
    print("case: empty, zero, absent and unreadable are not identities")
    check("empty value", steam_appid({"STEAMAPPID": ""}), None)
    check("zero value", steam_appid({"STEAMAPPID": "0"}), None)
    check("both empty", steam_appid({"STEAMAPPID": "", "STEAMGAMEID": ""}), None)
    check("empty SteamAppId falls through to SteamGameId",
          steam_appid({"STEAMAPPID": "", "STEAMGAMEID": "1234"}), "1234")
    check("zero SteamAppId falls through to SteamGameId",
          steam_appid({"STEAMAPPID": "0", "STEAMGAMEID": "1234"}), "1234")
    check("no steam keys", steam_appid({"SYSTEMROOT": "C:\\WINDOWS"}), None)
    check("empty mapping", steam_appid({}), None)
    check("unreadable environment", steam_appid(None), None)
    # "NonSteam" is a real answer: a library-added title is still the user's
    # game, and the caller uses the value to name it.
    check("NonSteam is an identity", steam_appid({"STEAMGAMEID": "NonSteam"}), "NonSteam")


def case_live_child_environment() -> None:
    print("case: a live child's environment, through the real psutil reader")
    child = psutil.Popen(
        [sys.executable, "-c", "import time; time.sleep(6)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={"STEAMAPPID": "730", "PATH": ""},
    )
    try:
        try:
            env = child.environ()
        except psutil.AccessDenied:
            print("  SKIP live environ read: this process may not read child "
                  "environments (not counted as a pass)")
            return
        check("psutil handed back a mapping", isinstance(env, dict), True)
        upper = [k for k in env if k.upper() == "STEAMAPPID"]
        check("it names the key exactly once", len(upper), 1)
        # The whole point: the key as delivered is not the lower-case spelling.
        check("the delivered key is upper-cased, as Windows parses it",
              upper[0], "STEAMAPPID")
        check("and identity reads it anyway", steam_appid(env), "730")
    finally:
        child.terminate()
        try:
            child.wait(5)
        except psutil.TimeoutExpired:
            child.kill()


def case_gone_process_is_unknown() -> None:
    print("case: a process that vanished is unavailable, not 'not a game'")
    child = psutil.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    pid = child.pid
    child.wait(10)
    check("the pid is gone", psutil.pid_exists(pid), False)
    # SteamIdentity must not turn a failed handle read into evidence of absence;
    # it reports nothing, which is what lets the present stream stay the judge.
    from app.steamid import SteamIdentity
    ident = SteamIdentity(refresh_s=0.0)
    check("unknown pid asks for nothing", ident.appid(pid), None)
    check("and is not called a game", ident.is_steam_game(pid), False)


def main() -> int:
    case_windows_uppercase_keys()
    case_mixed_case_and_spellings()
    case_not_an_identity()
    case_live_child_environment()
    case_gone_process_is_unknown()
    print("\nSELFTEST " + ("PASSED" if not fails else f"FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
