from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

API_ROOT = "https://api.github.com"
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_README_BYTES = 128 * 1024
PLUGIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
CATEGORY = re.compile(r"^[a-z0-9-]{1,32}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SIGNER_WORKFLOW = (
    "WinIslandProject/PluginMarketplace/.github/workflows/build-plugin.yml"
)
SUPPORTED_ABI_VERSIONS = frozenset({1, 2})


@dataclass(frozen=True)
class Registration:
    id: str
    repository: str
    asset: str
    categories: tuple[str, ...]
    min_winisland_version: str


@dataclass(frozen=True)
class ValidatedRelease:
    registration: Registration
    name: str
    author: str
    version: str
    description: str
    github_link: str
    abi_version: int
    download_url: str
    source_commit: str
    released_at: str
    sha256: str
    size: int
    readme: str
    icon_name: str | None
    icon: bytes | None


def github_json(path: str) -> object:
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "WinIsland-PluginMarketplace",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub API {path} returned {error.code}: {body}") from error


def parse_registration(path: Path) -> Registration:
    with path.open("rb") as file:
        value = tomllib.load(file)
    allowed = {
        "schema",
        "id",
        "repository",
        "asset",
        "categories",
        "min_winisland_version",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{path}: unknown keys: {', '.join(unknown)}")
    if value.get("schema") != 1:
        raise ValueError(f"{path}: schema must be 1")
    plugin_id = value.get("id")
    repository = value.get("repository")
    asset = value.get("asset", "*.winisland-plugin.zip")
    categories = value.get("categories", [])
    minimum = value.get("min_winisland_version", "1.3.0")
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError(f"{path}: invalid plugin id")
    if path.stem != plugin_id:
        raise ValueError(f"{path}: file name must match id '{plugin_id}'")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError(f"{path}: repository must be owner/name")
    if (
        not isinstance(asset, str)
        or not asset
        or "/" in asset
        or "\\" in asset
        or not asset.lower().endswith(".zip")
    ):
        raise ValueError(f"{path}: asset must be a ZIP file-name glob")
    if not isinstance(categories, list) or not all(
        isinstance(category, str) and CATEGORY.fullmatch(category)
        for category in categories
    ):
        raise ValueError(f"{path}: invalid categories")
    if len(categories) != len(set(categories)):
        raise ValueError(f"{path}: duplicate categories")
    if not isinstance(minimum, str) or not VERSION.fullmatch(minimum):
        raise ValueError(f"{path}: invalid min_winisland_version")
    return Registration(
        id=plugin_id,
        repository=repository,
        asset=asset,
        categories=tuple(categories),
        min_winisland_version=minimum,
    )


def registrations(root: Path) -> list[Registration]:
    entries = [parse_registration(path) for path in sorted((root / "plugins").glob("*.toml"))]
    ids = [entry.id.casefold() for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("plugin IDs collide after case normalization")
    return entries


def validate_repository(registration: Registration) -> None:
    repository = github_json(f"/repos/{registration.repository}")
    if not isinstance(repository, dict) or repository.get("private") is not False:
        raise ValueError(f"{registration.id}: source repository must be public")
    if repository.get("archived") is True:
        raise ValueError(f"{registration.id}: source repository is archived")
    license_info = github_json(f"/repos/{registration.repository}/license")
    detected = license_info.get("license") if isinstance(license_info, dict) else None
    spdx = detected.get("spdx_id") if isinstance(detected, dict) else None
    if not isinstance(spdx, str) or spdx in {"", "NOASSERTION", "Other"}:
        raise ValueError(f"{registration.id}: GitHub must detect an SPDX license")


def download_asset(url: str, output: Path, expected_size: int) -> None:
    if expected_size <= 0 or expected_size > MAX_PACKAGE_BYTES:
        raise ValueError(f"release asset size must be between 1 and {MAX_PACKAGE_BYTES}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "WinIsland-PluginMarketplace"},
    )
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, output.open("wb") as file:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("release asset exceeds size limit")
            file.write(chunk)
    if total != expected_size:
        raise ValueError(f"release asset size changed: expected {expected_size}, got {total}")


def parse_manifest(text: str) -> dict[str, object]:
    result: dict[str, object] = {}
    current_list: list[str] | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")):
            raise ValueError("plugin.yml must use a flat manifest")
        if raw_line.startswith("-"):
            if current_list is None:
                raise ValueError("plugin.yml contains an unexpected list item")
            current_list.append(raw_line[1:].strip().strip("'\""))
            continue
        if ":" not in raw_line:
            raise ValueError("plugin.yml contains an invalid line")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not raw_value:
            current_list = []
            result[key] = current_list
            continue
        current_list = None
        if raw_value.isdigit():
            result[key] = int(raw_value)
        else:
            result[key] = raw_value.strip("'\"")
    return result


def safe_zip_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or ":" in name:
        return False
    parts = name.replace("\\", "/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def validate_attestation(package: Path, repository: str) -> None:
    subprocess.run(
        [
            "gh",
            "attestation",
            "verify",
            str(package),
            "--repo",
            repository,
            "--signer-workflow",
            SIGNER_WORKFLOW,
            "--deny-self-hosted-runners",
        ],
        check=True,
    )


def resolve_tag_commit(repository: str, tag_name: str) -> str:
    tag = github_json(f"/repos/{repository}/git/ref/tags/{tag_name}")
    if not isinstance(tag, dict):
        return ""
    obj = tag.get("object")
    for _ in range(8):
        if not isinstance(obj, dict):
            return ""
        object_type = obj.get("type")
        sha = obj.get("sha")
        if not isinstance(sha, str):
            return ""
        if object_type == "commit":
            return sha
        if object_type != "tag":
            return ""
        tag_object = github_json(f"/repos/{repository}/git/tags/{sha}")
        obj = tag_object.get("object") if isinstance(tag_object, dict) else None
    raise ValueError(f"{repository}: tag nesting exceeds the supported limit")


def validate_release(registration: Registration, verify_attestation: bool = True) -> ValidatedRelease:
    validate_repository(registration)
    release = github_json(f"/repos/{registration.repository}/releases/latest")
    if not isinstance(release, dict) or release.get("draft") is True:
        raise ValueError(f"{registration.id}: latest release is unavailable")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"{registration.id}: release assets are missing")
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and fnmatch.fnmatchcase(asset["name"], registration.asset)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{registration.id}: expected one asset matching {registration.asset}, got {len(matches)}"
        )
    asset = matches[0]
    url = asset.get("browser_download_url")
    size = asset.get("size")
    if not isinstance(url, str) or not isinstance(size, int):
        raise ValueError(f"{registration.id}: invalid release asset metadata")
    target_commit = release.get("target_commitish")
    tag_name = release.get("tag_name")
    if not isinstance(tag_name, str):
        raise ValueError(f"{registration.id}: release tag is missing")
    source_commit = resolve_tag_commit(registration.repository, tag_name)
    if not source_commit and isinstance(target_commit, str):
        commit = github_json(f"/repos/{registration.repository}/commits/{target_commit}")
        if isinstance(commit, dict) and isinstance(commit.get("sha"), str):
            source_commit = commit["sha"]
    if not source_commit:
        raise ValueError(f"{registration.id}: cannot resolve source commit")

    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / asset["name"]
        download_asset(url, package, size)
        if verify_attestation:
            validate_attestation(package, registration.repository)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            if any(not safe_zip_member(name) for name in names):
                raise ValueError(f"{registration.id}: package contains an unsafe path")
            if names.count("plugin.yml") != 1:
                raise ValueError(f"{registration.id}: root plugin.yml is required")
            manifest_info = archive.getinfo("plugin.yml")
            if manifest_info.file_size > 1024 * 1024:
                raise ValueError(f"{registration.id}: plugin.yml is too large")
            manifest = parse_manifest(archive.read("plugin.yml").decode("utf-8"))
            expected_repo = f"https://github.com/{registration.repository}"
            if manifest.get("id") != registration.id:
                raise ValueError(f"{registration.id}: manifest ID does not match")
            if manifest.get("github-link", "").rstrip("/") != expected_repo:
                raise ValueError(f"{registration.id}: manifest repository does not match")
            abi_version = manifest.get("abi-version")
            if abi_version not in SUPPORTED_ABI_VERSIONS:
                supported = ", ".join(
                    f"v{version}" for version in sorted(SUPPORTED_ABI_VERSIONS)
                )
                raise ValueError(
                    f"{registration.id}: supported plugin ABIs are {supported}"
                )
            required = ["name", "author", "version", "description"]
            if any(not isinstance(manifest.get(key), str) or not manifest[key] for key in required):
                raise ValueError(f"{registration.id}: manifest metadata is incomplete")
            readme = ""
            readme_name = manifest.get("readme")
            if isinstance(readme_name, str):
                info = archive.getinfo(readme_name)
                if info.file_size > MAX_README_BYTES:
                    raise ValueError(f"{registration.id}: marketplace README exceeds 128 KiB")
                readme = archive.read(readme_name).decode("utf-8")
            icon_name = manifest.get("icon")
            icon = None
            if isinstance(icon_name, str):
                info = archive.getinfo(icon_name)
                if info.file_size > 4 * 1024 * 1024:
                    raise ValueError(f"{registration.id}: icon exceeds 4 MiB")
                icon = archive.read(icon_name)
            else:
                icon_name = None

    return ValidatedRelease(
        registration=registration,
        name=str(manifest["name"]),
        author=str(manifest["author"]),
        version=str(manifest["version"]),
        description=str(manifest["description"]),
        github_link=str(manifest["github-link"]),
        abi_version=int(manifest["abi-version"]),
        download_url=url,
        source_commit=source_commit,
        released_at=str(release.get("published_at", "")),
        sha256=digest,
        size=size,
        readme=readme,
        icon_name=icon_name,
        icon=icon,
    )
