# GitHub Backup for Home Assistant

A Home Assistant add-on that backs up an explicit whitelist of files and
directories to a GitHub repository on a cron schedule. Each run creates one
atomic Git commit, so a backup never contains a partially updated set of files.

## Features

- Backs up individual files or recursively backs up directories.
- Allows only paths under Home Assistant's `/config`, `/share`, `/ssl`, and
  `/media` mounts.
- Uses a configured repository subdirectory to avoid touching unrelated files.
- Removes files that were previously backed up but are no longer in the
  whitelist or source directory.
- Creates a manifest in the backup directory to precisely identify managed
  remote files.
- Uses native Git commits and pushes; unchanged backups create no commit and
  only changed content is transferred after the initial backup.

## Installation

1. Create a **private** GitHub repository for the backups. Do not add sensitive
   backup data to a public repository.
2. Create a fine-grained GitHub personal access token with **Contents:
   Read and write** permission for that repository. The selected repository
   must already contain the branch you configure (for example, create an
   initial README commit on `main`).
3. In Home Assistant, go to **Settings > Add-ons > Add-on Store**. Open the
   overflow menu, select **Repositories**, and add the URL of the repository
   containing this add-on.
4. Find **GitHub Backup**, select **Install**, then open its **Configuration**
   tab.
5. Enter the non-secret options described below, enter the token in the
   add-on's **GitHub token** password field, click **Save**, and start the
   add-on.

## Configuration

```yaml
repository: foundation50/home-assistant-config
branch: main
schedule: "0 3 * * *"
whitelist:
  - /config/configuration.yaml
  - /config/automations.yaml
  - /config/custom_components
backup_path: home-assistant
run_on_startup: true
```

| Option | Description |
| --- | --- |
| `repository` | Target repository in `OWNER/REPOSITORY` form. |
| `branch` | Existing branch to update. |
| `schedule` | Five-field cron expression in the Home Assistant host's local time: minute, hour, day of month, month, day of week. |
| `whitelist` | Non-empty list of absolute files or directories below `/config`, `/share`, `/ssl`, or `/media`. Directories are copied recursively. |
| `backup_path` | Relative directory in the repository used exclusively for this add-on's backups. |
| `run_on_startup` | Runs a backup immediately after the add-on starts. |
| `github_token` | Optional fallback GitHub fine-grained token with repository Contents read/write access. Home Assistant treats this as a password field; it is stored in the Supervisor-managed add-on options, not in this repository. |

For example, `0 3 * * *` runs every day at 03:00, and `*/30 * * * *` runs
every 30 minutes. Day-of-week numbers use cron convention: `0` is Sunday.

### Supplying the token

The checked-in [config.yaml](./config.yaml) is an add-on manifest, not an
instance configuration file. It intentionally contains no GitHub token and is
safe to commit. At runtime, the add-on resolves credentials in this order:

1. `GITHUB_TOKEN` environment variable, when the deployment injects it.
2. The `github_token` password field saved in Home Assistant's add-on
   configuration.

The standard Home Assistant add-on UI does not provide arbitrary environment
variable injection. For a normal Home Assistant installation, use the password
field and do not copy the token into this repository, a YAML file, or a
`.env` file. Deployments that manage container environment variables externally
can inject `GITHUB_TOKEN`; this is useful for secret managers and automated
deployments.

## Operation

The source filesystem layout is retained below `backup_path`; for example,
`/config/configuration.yaml` becomes
`home-assistant/config/configuration.yaml` in the GitHub repository.

The add-on writes `backup_path/.ha-github-backup-manifest.json`. Do not edit or
delete that file manually: it identifies exactly which remote files the add-on
may remove when they disappear locally. The add-on does not alter files outside
`backup_path`.

Each run fetches the configured branch into a temporary local worktree, applies
the backup there, and stages only `backup_path`. If staged contents are
unchanged, the add-on does not create a commit. Otherwise, it creates one commit
and pushes it with Git's compressed pack protocol. A concurrent update to the
same branch causes the push to fail rather than overwriting that update; the
add-on reports the error and retries on its next scheduled run.

To restore a file, retrieve it from a GitHub commit and copy it back to its
original path under Home Assistant. After restoring configuration, validate it
and restart Home Assistant as appropriate.

## Security

Use a dedicated, private repository and a fine-grained token limited to that
repository. A backup can contain credentials, certificates, and other secrets.
Restrict the whitelist to only the files you intend to store, and rotate the
GitHub token immediately if it is exposed.
