# WinIsland Plugin Marketplace

This repository is the curated source registry for the WinIsland plugin market.

## Add a plugin

1. Keep the plugin source in a public GitHub repository with a detected
   open-source license.
2. Publish the plugin through the reusable WinIsland build workflow.
3. Add one `plugins/<plugin-id>.toml` file in a pull request.
4. Wait for the automated checks and maintainer review.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete format and release setup.

The marketplace validates both ABI v1 and ABI v2 plugin packages. New plugins
should use ABI v2, which is the current WinIsland host ABI; clients filter
catalog entries whose ABI does not match the running host.
