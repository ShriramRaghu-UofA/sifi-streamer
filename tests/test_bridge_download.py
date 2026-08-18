import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

from sifi_streamer.sifi.bridge_install import (
    MANIFEST_NAME,
    TESTED_VERSION,
    BridgeAsset,
    BridgeDownloadError,
    _download,
    _extract_executable,
    build_parser,
    install_bridge,
    latest_asset,
    platform_asset_suffix,
    tagged_asset,
    tested_asset,
)


def zip_archive(payload: bytes = b"windows executable") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sifibridge-release/sifibridge.exe", payload)
    return buffer.getvalue()


def tar_archive(payload: bytes = b"unix executable") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("sifibridge-release/sifibridge")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class BridgeDownloadTests(unittest.TestCase):
    def test_download_streams_and_hashes_with_urllib(self) -> None:
        payload = b"verified archive bytes"
        asset = BridgeAsset(
            "version",
            "bridge.zip",
            "https://example.test/bridge.zip",
            hashlib.sha256(payload).hexdigest(),
            "tag",
        )

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_val: BaseException | None,
                exc_tb: TracebackType | None,
            ) -> None:
                self.close()

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bridge.zip"
            with patch(
                "sifi_streamer.sifi.bridge_install.urlopen",
                return_value=Response(payload),
            ) as open_url:
                digest = _download(asset, destination)
            self.assertEqual(digest, asset.sha256)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(open_url.call_args.kwargs["timeout"], 60)

    def test_platform_detection(self) -> None:
        cases = (
            ("Windows", "AMD64", "x86_64-pc-windows-msvc.zip"),
            ("Darwin", "arm64", "aarch64-apple-darwin.tar.gz"),
            ("Darwin", "x86_64", "x86_64-apple-darwin.tar.gz"),
            ("Linux", "aarch64", "aarch64-unknown-linux-gnu.tar.gz"),
            ("Linux", "x86_64", "x86_64-unknown-linux-gnu.tar.gz"),
        )
        for system, machine, expected in cases:
            with self.subTest(system=system, machine=machine):
                self.assertEqual(platform_asset_suffix(system, machine), expected)
        with self.assertRaises(BridgeDownloadError):
            platform_asset_suffix("Windows", "arm64")

    def test_tested_release_uses_pinned_version_and_digest(self) -> None:
        asset = tested_asset(suffix="x86_64-pc-windows-msvc.zip")
        self.assertEqual(asset.version, TESTED_VERSION)
        self.assertEqual(
            asset.sha256,
            "430695acae73d0cf8f199d00462d93c4e6c9bad0eea96f8e6dada7a7aeef9ff1",
        )
        self.assertTrue(asset.url.endswith(f"/{asset.filename}"))

    def test_latest_selects_platform_asset_and_requires_digest(self) -> None:
        release = {
            "tag_name": "2.1.0",
            "assets": [
                {
                    "name": "sifibridge-2.1.0-x86_64-pc-windows-msvc.zip",
                    "browser_download_url": "https://example.test/bridge.zip",
                    "digest": "sha256:" + "a" * 64,
                },
                {
                    "name": "sifibridge-2.1.0-x86_64-unknown-linux-gnu.tar.gz",
                    "browser_download_url": "https://example.test/bridge.tar.gz",
                    "digest": "sha256:" + "b" * 64,
                },
            ],
        }
        with patch(
            "sifi_streamer.sifi.bridge_install._read_json", return_value=release
        ):
            asset = latest_asset(suffix="x86_64-pc-windows-msvc.zip")
        self.assertEqual(asset.version, "2.1.0")
        self.assertEqual(asset.sha256, "a" * 64)
        self.assertEqual(asset.source, "latest")

        release_without_digest = {
            "tag_name": "2.1.0",
            "assets": [
                {
                    "name": "sifibridge-2.1.0-x86_64-pc-windows-msvc.zip",
                    "browser_download_url": "https://example.test/bridge.zip",
                    "digest": None,
                }
            ],
        }
        with (
            patch(
                "sifi_streamer.sifi.bridge_install._read_json",
                return_value=release_without_digest,
            ),
            self.assertRaisesRegex(BridgeDownloadError, "no valid SHA-256"),
        ):
            latest_asset(suffix="x86_64-pc-windows-msvc.zip")

    def test_specific_tag_uses_tag_metadata_and_requires_matching_tag(self) -> None:
        release = {
            "tag_name": "2.0.0-b20",
            "assets": [
                {
                    "name": "sifibridge-2.0.0-b20-x86_64-pc-windows-msvc.zip",
                    "browser_download_url": "https://example.test/tagged.zip",
                    "digest": "sha256:" + "c" * 64,
                }
            ],
        }
        with patch(
            "sifi_streamer.sifi.bridge_install._read_json", return_value=release
        ) as read_json:
            asset = tagged_asset("2.0.0-b20", suffix="x86_64-pc-windows-msvc.zip")
        self.assertTrue(read_json.call_args.args[0].endswith("/2.0.0-b20"))
        self.assertEqual(asset.version, "2.0.0-b20")
        self.assertEqual(asset.source, "tag")
        self.assertEqual(asset.sha256, "c" * 64)

        release["tag_name"] = "unexpected"
        with (
            patch("sifi_streamer.sifi.bridge_install._read_json", return_value=release),
            self.assertRaisesRegex(BridgeDownloadError, "GitHub returned"),
        ):
            tagged_asset("2.0.0-b20", suffix="x86_64-pc-windows-msvc.zip")

    def test_tag_rejects_path_like_values(self) -> None:
        for tag in ("", " ../release", "owner/release", r"owner\release"):
            with self.subTest(tag=tag), self.assertRaises(BridgeDownloadError):
                tagged_asset(tag, suffix="x86_64-pc-windows-msvc.zip")

    def test_nested_zip_is_verified_installed_and_manifested(self) -> None:
        archive = zip_archive()
        digest = hashlib.sha256(archive).hexdigest()
        asset = BridgeAsset(
            TESTED_VERSION,
            f"sifibridge-{TESTED_VERSION}-x86_64-pc-windows-msvc.zip",
            "https://example.test/bridge.zip",
            digest,
            "tested",
        )

        def fake_download(_: BridgeAsset, destination: Path) -> str:
            destination.write_bytes(archive)
            return digest

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "chosen-location"
            with (
                patch(
                    "sifi_streamer.sifi.bridge_install.tested_asset",
                    return_value=asset,
                ),
                patch(
                    "sifi_streamer.sifi.bridge_install._download",
                    side_effect=fake_download,
                ),
            ):
                executable, manifest_path = install_bridge(output)
            self.assertEqual(executable.read_bytes(), b"windows executable")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], TESTED_VERSION)
            self.assertEqual(manifest["sha256"], digest)
            self.assertEqual(manifest["asset"], asset.filename)
            self.assertEqual(manifest["executable"], "sifibridge.exe")
            self.assertEqual(manifest_path.name, MANIFEST_NAME)
            with (
                patch(
                    "sifi_streamer.sifi.bridge_install.tested_asset",
                    return_value=asset,
                ),
                self.assertRaisesRegex(BridgeDownloadError, "Refusing to overwrite"),
            ):
                install_bridge(output)

    def test_checksum_mismatch_installs_nothing(self) -> None:
        archive = zip_archive()
        asset = BridgeAsset(
            "bad",
            "sifibridge-bad-x86_64-pc-windows-msvc.zip",
            "https://example.test/bad.zip",
            "0" * 64,
            "latest",
        )

        def fake_download(_: BridgeAsset, destination: Path) -> str:
            destination.write_bytes(archive)
            return hashlib.sha256(archive).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with (
                patch(
                    "sifi_streamer.sifi.bridge_install.latest_asset",
                    return_value=asset,
                ),
                patch(
                    "sifi_streamer.sifi.bridge_install._download",
                    side_effect=fake_download,
                ),
                self.assertRaisesRegex(BridgeDownloadError, "SHA-256 mismatch"),
            ):
                install_bridge(output, latest=True)
            self.assertFalse((output / "sifibridge.exe").exists())
            self.assertFalse((output / MANIFEST_NAME).exists())

    def test_nested_tar_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bridge.tar.gz"
            archive.write_bytes(tar_archive())
            self.assertEqual(
                _extract_executable(
                    archive,
                    "sifibridge-version-x86_64-unknown-linux-gnu.tar.gz",
                ),
                b"unix executable",
            )

    def test_release_selection_flags_conflict(self) -> None:
        for arguments in (
            ["--tested", "--latest"],
            ["--tested", "--tag", "2.0.0-b20"],
            ["--latest", "--tag", "2.0.0-b20"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch("sys.stderr"),
                self.assertRaises(SystemExit),
            ):
                build_parser().parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
