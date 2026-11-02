# Install SiFi Bridge

For hardware capture, install the bridge explicitly after setting up the Python
package. Commands below run from your project or repository root with `uv`.
Read the vendor notice before downloading. Return to the
[capture guide](capture-guide.md) to connect and record.

## Vendor dependency and license

This project uses [SiFi Bridge](https://github.com/SiFiLabs/sifi-bridge-pub),
proprietary vendor software provided by **SiFi Labs Inc.** SiFi Bridge is free
to use but is not open-source software. It is distributed separately and is
not covered by this repository's MIT License. Software built on it should
credit it as "SiFi Bridge (SiFi Labs Inc.)." The tested bridge release is
pinned below because the bridge is evolving and later releases may
introduce incompatible changes.

SiFi Labs Inc. grants permission to download, install, and run the SiFi Bridge
CLI, including in academic, commercial, and publicly demonstrated projects,
subject to these terms:

- Redistribute the binary only unmodified and with this notice included;
  linking to the [official release](https://github.com/SiFiLabs/sifi-bridge-pub/releases)
  is preferred.
- Do not modify, decompile, reverse engineer, or attempt to derive the source
  code of the binary.
- Including SiFi Bridge in an open-source project does not place it under that
  project's license.
- Use of the SiFi Labs name or logo in a way that suggests endorsement,
  sponsorship, or validation requires written permission.
- SiFi devices and SiFi Bridge are intended for research and development use.
  They are not medical devices and must not be used for diagnosis, treatment,
  or any application where failure could cause injury.
- SiFi Bridge is provided as is, without warranty of any kind. SiFi Labs Inc.
  accepts no liability for its use.

Questions about a specific use case should be sent to
[contact@sifilabs.com](mailto:contact@sifilabs.com).

## Download and verify

Bridge acquisition is always an explicit user action. Installing this package
or starting a capture never downloads or updates vendor software.

Install the pinned `2.0.1` release into the default `bin`
directory:

```powershell
uv run sifi-download-bridge --tested
```

Choose another destination if desired:

```powershell
uv run sifi-download-bridge --tested --output-directory C:\tools\sifi
```

The latest GitHub release may also work, but it has not necessarily been tested
with this package and therefore requires an explicit opt-in:

```powershell
uv run sifi-download-bridge --latest --output-directory C:\tools\sifi
```

A specific untested release tag can be selected explicitly:

```powershell
uv run sifi-download-bridge --tag 2.0.1 --output-directory C:\tools\sifi
```

The utility auto-detects supported Windows, macOS, and Linux architectures,
uses only Python's standard-library `urllib`, verifies the release asset's
SHA-256 before installation, and extracts the nested `sifibridge` executable.
Capture commands default to `bin/sifibridge.exe` on Windows and
`bin/sifibridge` on macOS and Linux.
It writes `sifibridge-manifest.json` beside the executable with the release
version, asset, verified SHA-256, source URL, and installation time. Latest and
specific-tag modes refuse to install an asset if GitHub does not publish a
valid SHA-256 digest. Existing files are not replaced unless `--force` is
supplied. Tested mode uses the same tag resolver with the version and hashes
pinned by the maintainer.
