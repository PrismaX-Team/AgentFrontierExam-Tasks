# When the task needs extra software

The empty package template uses the operator default image:

```json
{"schema_version": "1.0", "type": "default"}
```

That directory may contain **only** `environment.json`. Keep it until every
extra package has a name **and** a frozen version. A proposal that only
names software is not enough.

When the pins exist, replace `task_package/environment/` with the two files
in this folder:

1. `environment.json` — `type` is `containerfile`.
2. `Containerfile` — starts from `SIE_BASE_IMAGE`, installs the packages.

Then list the same software in `instruction.json.environment` so the agent
knows it is already installed. Example:

```json
"environment": [
  {
    "name": "PyTorch",
    "version": "2.4.1",
    "description": "CPU wheel already installed in the task image."
  }
]
```

`instruction.json.environment` does not install anything. `environment/` does.
Do not write a sentence that only says “use the default image”.
