"""Steam identity: which running processes are Steam games, and which AppID.

Steam's own launch contract: every process `steam.exe` starts gets
`SteamAppId`/`SteamGameId` in its environment (the numeric AppID, or
"NonSteam" for titles added to the library), inherited by all child processes,
so launcher→game chains carry it too. That is a documented client behavior,
not a heuristic — unlike "borderless window with GPU load".

This is deliberately an *enrichment* layer, never the detection core: anti-cheat
protected processes refuse the handle read, and games launched directly from
their exe bypass Steam. Both cases still present frames, so gamewatch treats
this as preference/identity, with present+foreground as the backbone.
"""
from __future__ import annotations

import time

import psutil

_STEAM_ENV = ("steamappid", "steamgameid")


class SteamIdentity:
    def __init__(self, refresh_s: float = 2.0):
        self._refresh_s = refresh_s
        self._next = 0.0
        self._games: dict[int, str] = {}    # pid -> appid ("730", "NonSteam", ...)
        self._steam_pids: set[int] = set()

    def _refresh(self) -> None:
        procs = {}
        steam = set()
        for p in psutil.process_iter(["pid", "ppid", "name"]):
            try:
                procs[p.info["pid"]] = (p.info["ppid"], (p.info["name"] or "").lower())
            except psutil.Error:
                continue
            if (p.info["name"] or "").lower() == "steam.exe":
                steam.add(p.info["pid"])
        self._steam_pids = {p for p in steam if p in procs}

        def under_steam(pid: int) -> bool:
            for _ in range(6):  # launcher chains are shallow
                ent = procs.get(pid)
                if ent is None:
                    return False
                if ent[0] in self._steam_pids:
                    return True
                pid = ent[0]
            return False

        games: dict[int, str] = {}
        for pid in procs:
            if not under_steam(pid):
                continue
            try:
                env = psutil.Process(pid).environ()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue  # EAC/BE protected, or exited mid-scan — present-stream still sees it
            appid = next((env[k] for k in _STEAM_ENV if env.get(k, "") not in ("", "0")), None)
            if appid:
                games[pid] = appid
        self._games = games

    def appid(self, pid: int) -> str | None:
        """Steam AppID for a pid (or None if it isn't Steam-launched).
        Refreshes at most once per `refresh_s` — cheap process-tree walk."""
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self._refresh_s
            try:
                self._refresh()
            except psutil.Error:
                pass
        return self._games.get(pid)

    def is_steam_game(self, pid: int) -> bool:
        return self.appid(pid) is not None

    def games(self) -> dict[int, str]:
        self.appid(-1)  # piggyback the throttled refresh
        return dict(self._games)
