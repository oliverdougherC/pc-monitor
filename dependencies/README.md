# Downloaded dependency

`scripts/Fetch-Dependencies.ps1` obtains PresentMon 2.5.1 from its upstream release
and verifies SHA-256 `9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191`.
The executable is ignored by Git and copied into the published installation.
Its MIT license is tracked here. All other dependencies use NuGet lock files.
