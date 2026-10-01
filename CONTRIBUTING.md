# Contributing a plugin

The marketplace is designed so maintainers only need to review and merge a
small registration pull request. Build and release metadata is discovered and
validated automatically.

## Repository requirements

- The complete plugin source must be public.
- GitHub must detect a valid SPDX open-source license for the repository.
- The repository must publish a WinIsland ZIP through the official reusable
  workflow in this repository.
- The ZIP `plugin.yml` ID and repository link must match the registration.
- The release asset must be immutable for a version. Publish a new version
  instead of replacing a published package.
- The plugin must not be obfuscated or download executable code at runtime.
- The release validator accepts ABI v1 and ABI v2 manifests. New plugins
  should target ABI v2, which is the current WinIsland host ABI; the catalog
  records the package ABI so hosts can filter incompatible entries.

## Registration format

Create `plugins/<plugin-id>.toml`:

```toml
schema = 1
id = "example-clock"
repository = "owner/example-clock"
asset = "*.winisland-plugin.zip"
categories = ["widget", "utility"]
min_winisland_version = "1.3.0"
```

Rules:

- A contributor pull request may add exactly one registration file and may not
  modify workflows, scripts, catalog output, or existing registrations.
- `id` must equal the file name and the package manifest ID.
- `repository` is the GitHub `owner/name` pair, not a URL.
- `asset` is a file-name glob without path separators. The default official
  workflow produces `*.winisland-plugin.zip`.
- Categories are lowercase identifiers. Use only categories already present in
  the registry unless a maintainer approves a new one.
- Updating the plugin does not require a marketplace pull request. Publishing a
  new valid GitHub Release is enough; the catalog refresh finds it automatically.

## Plugin release workflow

Add this workflow to the plugin repository as
`.github/workflows/release.yml`:

```yaml
name: Release WinIsland plugin

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write
  id-token: write
  attestations: write

jobs:
  release:
    uses: WinIslandProject/PluginMarketplace/.github/workflows/build-plugin.yml@main
```

For production use, pin the reusable workflow to a reviewed full commit SHA.
The workflow expects the plugin to use the official packager example:

```powershell
cargo run --example pack
```

It checks, lints, builds, packages, attests, and uploads the ZIP to the tag's
GitHub Release.

## Review policy

Automated validation is necessary but not sufficient. Maintainers review the
source, declared capabilities, network behavior, update behavior, bundled DLLs,
and license before merging a first submission. A plugin or version can be
revoked later without deleting its registration.
