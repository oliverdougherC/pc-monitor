# Third-party components

PC Monitor is distributed under GPL-3.0-or-later; see LICENSE.

- The revision-C packet format and serialization in `Panel.cs` are adapted from
  [turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python),
  commit `2b33ab4f00a096916dd6a1174441a53a7ec33b03`, GPL-3.0-or-later.
  Copyright Matthieu Houdebine (2021), Alex W. Baulé (2023), Arthur Ferrai (2023).
  Reference-packet fixtures were generated from that revision without opening hardware.
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  0.9.6 is an unmodified NuGet dependency, MPL-2.0, with transitive dependencies
  recorded in `packages.lock.json`. It supplies hardware sensors and its driver.
- [PresentMon](https://github.com/GameTechDev/PresentMon) 2.5.1, MIT, is downloaded
  separately and verified by SHA-256 in `scripts/Fetch-Dependencies.ps1`.
- JetBrains Mono ExtraBold: JetBrains, SIL Open Font License 1.1.
  Roboto Bold/Medium: Google, Apache License 2.0. Font license texts accompany assets.
- Microsoft .NET and System.IO.Ports: Microsoft/.NET contributors, MIT.

NuGet restores the exact dependency graph with content hashes. Build outputs include
the unmodified dependencies; upstream source and licensing are linked above.
