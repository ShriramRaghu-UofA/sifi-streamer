"""Explicit, checksum-verified installer for the SiFi bridge executable."""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sifi_streamer.exceptions import StreamerError

TESTED_VERSION = "2.0.0-b21"
REPOSITORY_URL = "https://github.com/SiFiLabs/sifi-bridge-pub"
LATEST_RELEASE_API = (
    "https://api.github.com/repos/SiFiLabs/sifi-bridge-pub/releases/latest"
)
MANIFEST_NAME = "sifibridge-manifest.json"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

_TESTED_SHA256 = {
    "aarch64-apple-darwin.tar.gz": (
        "a4fef1c924bb30784f4e0e73dbb767080445b3b0fec1e35929488963bbc5f9b1"
    ),
    "aarch64-unknown-linux-gnu.tar.gz": (
        "4e20e343c70394f3bb4ffc1cc390ff6882bca9106dc460112e3ddad120400e52"
    ),
    "x86_64-apple-darwin.tar.gz": (
        "ace77636bd9f5aafc0ef6b12c35a2dd8ea2bbbb369e89aa11cd14c31b35101f2"
    ),
    "x86_64-pc-windows-msvc.zip": (
        "430695acae73d0cf8f199d00462d93c4e6c9bad0eea96f8e6dada7a7aeef9ff1"
    ),
    "x86_64-unknown-linux-gnu.tar.gz": (
        "467e19d72143c6129ae427868ead0542fcb70b4969b8dcc15c2112e71c5e3b53"
    ),
}


class BridgeDownloadError(StreamerError):
    """A bridge release could not be resolved, verified, or installed."""


@dataclass(frozen=True, slots=True)
class BridgeAsset:
    """One release asset selected for the current platform."""

    version: str
    filename: str
    url: str
    sha256: str
    source: str


@dataclass(frozen=True, slots=True)
class BridgeInstallManifest:
    """Persistent provenance for an installed bridge executable."""

    schema_version: int
    version: str
    asset: str
    sha256: str
    source_url: str
    release_source: str
    executable: str
    installed_at_utc: str


def platform_asset_suffix(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Return the vendor asset suffix for a supported host platform."""
    operating_system = (system or platform.system()).lower()
    architecture = (machine or platform.machine()).lower()
    normalized_architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }.get(architecture, architecture)

    match operating_system, normalized_architecture:
        case "windows", "x86_64":
            return "x86_64-pc-windows-msvc.zip"
        case "darwin", "x86_64" | "aarch64":
            return f"{normalized_architecture}-apple-darwin.tar.gz"
        case "linux", "x86_64" | "aarch64":
            return f"{normalized_architecture}-unknown-linux-gnu.tar.gz"
        case _:
            raise BridgeDownloadError(
                "No SiFi bridge release asset is available for "
                f"{operating_system}/{architecture}"
            )


def tested_asset(*, suffix: str | None = None) -> BridgeAsset:
    """Resolve the maintainer-tested release for this platform."""
    selected_suffix = suffix or platform_asset_suffix()
    try:
        digest = _TESTED_SHA256[selected_suffix]
    except KeyError as exc:
        raise BridgeDownloadError(
            f"The tested release has no asset for {selected_suffix}"
        ) from exc
    return tagged_asset(
        TESTED_VERSION,
        suffix=selected_suffix,
        expected_sha256=digest,
        source="tested",
    )


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sifi-streamer-bridge-downloader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_json(url: str) -> dict[str, object]:
    try:
        with urlopen(_request(url), timeout=30) as response:
            value = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BridgeDownloadError(
            f"Unable to read GitHub release metadata: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise BridgeDownloadError("GitHub release metadata is not a JSON object")
    return value


def _asset_from_release(
    release: dict[str, object],
    suffix: str,
    *,
    source: str,
) -> BridgeAsset:
    """Select one platform asset from GitHub release metadata."""
    version = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(version, str) or not version:
        raise BridgeDownloadError("Latest release metadata has no tag_name")
    if not isinstance(assets, list):
        raise BridgeDownloadError("Latest release metadata has no asset list")

    candidates: list[dict[str, object]] = []
    for item in assets:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].endswith(suffix)
        ):
            candidates.append(item)
    if len(candidates) != 1:
        raise BridgeDownloadError(
            f"Expected one release asset ending in {suffix!r}; found {len(candidates)}"
        )
    selected = candidates[0]
    filename, url, digest = (
        selected.get("name"),
        selected.get("browser_download_url"),
        selected.get("digest"),
    )
    if not isinstance(filename, str) or not isinstance(url, str):
        raise BridgeDownloadError("Release asset metadata is incomplete")
    if Path(filename).name != filename:
        raise BridgeDownloadError("Latest release asset name is not a plain filename")
    if not isinstance(digest, str) or not re.fullmatch(
        r"sha256:[0-9a-fA-F]{64}", digest
    ):
        raise BridgeDownloadError(
            "Release asset has no valid SHA-256 digest; "
            "use --tested or wait for corrected release metadata"
        )
    return BridgeAsset(
        version, filename, url, digest.removeprefix("sha256:").lower(), source
    )


def tagged_asset(
    tag: str,
    *,
    suffix: str | None = None,
    expected_sha256: str | None = None,
    source: str = "tag",
) -> BridgeAsset:
    """Resolve one tagged release, optionally using a maintainer-pinned digest."""
    if not tag or tag.strip() != tag or any(character in tag for character in "/\\"):
        raise BridgeDownloadError("Release tag must be a non-empty plain tag name")
    selected_suffix = suffix or platform_asset_suffix()
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise BridgeDownloadError("Pinned SHA-256 digest is invalid")
        filename = f"sifibridge-{tag}-{selected_suffix}"
        return BridgeAsset(
            tag,
            filename,
            f"{REPOSITORY_URL}/releases/download/{quote(tag, safe='')}/{filename}",
            expected_sha256.lower(),
            source,
        )
    release_url = (
        "https://api.github.com/repos/SiFiLabs/sifi-bridge-pub/releases/tags/"
        f"{quote(tag, safe='')}"
    )
    asset = _asset_from_release(
        _read_json(release_url),
        selected_suffix,
        source=source,
    )
    if asset.version != tag:
        raise BridgeDownloadError(
            f"Requested tag {tag!r}, but GitHub returned {asset.version!r}"
        )
    return asset


def latest_asset(*, suffix: str | None = None) -> BridgeAsset:
    """Resolve the latest published asset and require GitHub's SHA-256 digest."""
    return _asset_from_release(
        _read_json(LATEST_RELEASE_API),
        suffix or platform_asset_suffix(),
        source="latest",
    )


def _download(asset: BridgeAsset, destination: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urlopen(_request(asset.url), timeout=60) as response,
            destination.open("xb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise BridgeDownloadError(
                        f"Download exceeds the {MAX_DOWNLOAD_BYTES}-byte safety limit"
                    )
                digest.update(chunk)
                output.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BridgeDownloadError(f"Unable to download {asset.url}: {exc}") from exc
    return digest.hexdigest()


def _archive_executable_name(filename: str) -> str:
    return "sifibridge.exe" if filename.endswith(".zip") else "sifibridge"


def _extract_executable(archive: Path, asset_filename: str) -> bytes:
    expected_name = _archive_executable_name(asset_filename)
    try:
        if asset_filename.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                candidates = [
                    member
                    for member in bundle.infolist()
                    if not member.is_dir()
                    and PurePosixPath(member.filename).name == expected_name
                ]
                if len(candidates) != 1:
                    raise BridgeDownloadError(
                        f"Archive contains {len(candidates)} {expected_name!r} files"
                    )
                if candidates[0].file_size > MAX_DOWNLOAD_BYTES:
                    raise BridgeDownloadError("Archived bridge executable is too large")
                return bundle.read(candidates[0])
        if asset_filename.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as bundle:
                candidates = [
                    member
                    for member in bundle.getmembers()
                    if member.isfile()
                    and PurePosixPath(member.name).name == expected_name
                ]
                if len(candidates) != 1:
                    raise BridgeDownloadError(
                        f"Archive contains {len(candidates)} {expected_name!r} files"
                    )
                if candidates[0].size > MAX_DOWNLOAD_BYTES:
                    raise BridgeDownloadError("Archived bridge executable is too large")
                extracted = bundle.extractfile(candidates[0])
                if extracted is None:
                    raise BridgeDownloadError("Unable to read bridge executable")
                return extracted.read()
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise BridgeDownloadError(f"Unable to read {asset_filename}: {exc}") from exc
    raise BridgeDownloadError(f"Unsupported bridge archive: {asset_filename}")


def install_bridge(
    output_directory: Path,
    *,
    latest: bool = False,
    tag: str | None = None,
    force: bool = False,
) -> tuple[Path, Path]:
    """Download, verify, and install a bridge selected for the current host."""
    if latest and tag is not None:
        raise BridgeDownloadError("latest and tag release selections conflict")
    if latest:
        asset = latest_asset()
    elif tag is not None:
        asset = tagged_asset(tag)
    else:
        asset = tested_asset()
    executable_name = _archive_executable_name(asset.filename)
    executable_path = output_directory / executable_name
    manifest_path = output_directory / MANIFEST_NAME
    if not force and (executable_path.exists() or manifest_path.exists()):
        raise BridgeDownloadError(
            f"Refusing to overwrite {executable_path} or {manifest_path}; use --force"
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sifi-bridge-download-") as temporary:
        archive = Path(temporary) / asset.filename
        actual_digest = _download(asset, archive)
        if not hmac.compare_digest(actual_digest, asset.sha256):
            raise BridgeDownloadError(
                f"SHA-256 mismatch for {asset.filename}: "
                f"expected {asset.sha256}, got {actual_digest}"
            )
        executable = _extract_executable(archive, asset.filename)

        temporary_executable = output_directory / f".{executable_name}.tmp"
        temporary_manifest = output_directory / f".{MANIFEST_NAME}.tmp"
        try:
            temporary_executable.write_bytes(executable)
            if os.name != "nt":
                temporary_executable.chmod(
                    temporary_executable.stat().st_mode
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )
            manifest = BridgeInstallManifest(
                schema_version=1,
                version=asset.version,
                asset=asset.filename,
                sha256=actual_digest,
                source_url=asset.url,
                release_source=asset.source,
                executable=executable_name,
                installed_at_utc=datetime.now(UTC).isoformat(),
            )
            temporary_manifest.write_text(
                json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary_executable.replace(executable_path)
            temporary_manifest.replace(manifest_path)
        finally:
            temporary_executable.unlink(missing_ok=True)
            temporary_manifest.unlink(missing_ok=True)
    return executable_path, manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly download and verify a SiFi bridge release."
    )
    release = parser.add_mutually_exclusive_group()
    release.add_argument(
        "--tested",
        action="store_true",
        help=f"Install maintainer-tested {TESTED_VERSION} (default)",
    )
    release.add_argument(
        "--latest",
        action="store_true",
        help="Install GitHub's latest release if it supplies a SHA-256 digest",
    )
    release.add_argument(
        "--tag",
        metavar="TAG",
        help="Install a specific GitHub release tag with a published SHA-256",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("bin"),
        help="Installation directory (default: bin)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing executable and manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        executable, manifest = install_bridge(
            args.output_directory,
            latest=args.latest,
            tag=args.tag,
            force=args.force,
        )
    except BridgeDownloadError as exc:
        raise SystemExit(f"Bridge download failed: {exc}") from exc
    print(f"Installed bridge: {executable}")
    print(f"Wrote manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
