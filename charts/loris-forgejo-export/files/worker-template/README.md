# @WORKER_REPOSITORY@: LORIS export site checks

This repository is managed by the central loris-forgejo-export reconciler. Its
workflows run on this site's worker (runner label `@RUNNER_LABEL@`) and call the
site command `@REQUEST_COMMAND@`. Only central operators dispatch them; dataset
owners publish from their dataset repositories, not from here.

| Workflow | Inputs | Result |
| --- | --- | --- |
| `probe` | none | Reports the site status, then checks each registered dataset; sends no content |
| `refresh` | `dataset` | Refreshes the dataset's metadata and file pointers |
| `stage-content` | `dataset`, `commit`, `keys` | Uploads the requested content; a missing file fails the job |

The organisation provides the variables `@VARIABLE_PREFIX@FORGE_URL` and
`@VARIABLE_PREFIX@FORGE_IP` and the secret `@VARIABLE_PREFIX@WORKER_TOKEN`.

Do not edit these files here: the reconciler restores them on its next run.
Never paste credentials in an issue or a job log.
