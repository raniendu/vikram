# Python extensions

Vikram extensions are explicitly installed Python packages. They run with the
person's normal host privileges in a separate worker process, with their own
Python environment. This is process and dependency isolation, not a security
sandbox. Install only code you trust. Project settings and model tool calls
cannot install packages. Scheduled runs and evaluations do not load extensions.

Use the extension controls to install, list, update, remove, enable or disable a
package. Sources may be a local directory, a local wheel, a PyPI requirement
(such as `example-vikram-extension==1.0.0`), or a Git HTTPS URL (such as
`git+https://example.org/team/extension.git@v1.0.0`). Git URLs cannot contain
credentials, query strings, or fragments. Private repositories that need an
interactive credential helper must be checked out separately and installed from
a local path. A local source directory is copied into the private environment;
subsequent source edits take effect only after an explicit update.

Installing or updating is an explicit authorization to execute that package's
installation and registration code. Vikram presents each model-invoked extension
operation for approval because it cannot classify arbitrary Python effects.
Without an approval-capable interface the operation is declined. Plan mode does
not start or invoke extensions. Enabling an extension authorizes its registered
lifecycle callbacks; those callbacks are trusted host code, too.

Each update builds and validates a fresh environment before atomically selecting
it. Failed updates leave the previous installation active. Previous environment
directories remain until the package is explicitly removed. Removing a package
that another Vikram instance is using is refused until that instance closes it.
An interrupted call is never automatically replayed. Restart a stopped worker by
reloading extensions or disabling and re-enabling the package.

## Local package

Create `vikram-extension.json` and a Python module in one directory:

```json
{
  "api_version": 1,
  "name": "example",
  "version": "1.0.0",
  "entry": "example:register",
  "dependencies": []
}
```

Names use a lowercase letter followed by lowercase letters, digits, or
underscores (48 characters maximum). Local packages may contain at most 2,000
regular files totaling 20 MiB. Symlinks are rejected; `.git`, `.venv`,
`__pycache__`, and `node_modules` are excluded. Dependencies are explicit PyPI
requirements and are installed only as part of the requested install/update.

```python
# example.py
from vikram.extension_sdk import Context, Extension


def register(extension: Extension) -> None:
    extension.register_setting("greeting", "Hello", description="Greeting text")

    async def greet(arguments: dict, context: Context) -> dict:
        message = f"{context.settings['greeting']}, {arguments['name']}"
        return {
            "output": message,
            "ui": [{"type": "text", "text": message}],
        }

    extension.register_tool(
        "greet",
        greet,
        description="Return a greeting for a name",
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 100}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    extension.register_command("hello", greet, schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    })

    async def completed(payload: dict, context: Context) -> dict:
        return {"ui": [{"type": "notice", "text": "Reply complete"}]}

    extension.register_hook("after_reply", completed)
```

The tool is exposed as `ext__example__greet`. Commands use the same prefix.
Registration is synchronous; tools, commands, and hooks may be synchronous or
async. Every callback receives its JSON arguments/payload and a `Context`.
`context.workspace` is supplied only for a trusted selected project.
`context.settings` contains registered defaults and explicit user settings.
Credential settings are unsupported.

Supported argument schema constraints are `type`, `properties`, `required`,
`additionalProperties` (boolean), `items`, `enum`, `minimum`, `maximum`,
`minLength`, `maxLength`, `minItems`, and `maxItems`. Descriptive `title`,
`description`, and `default` fields are allowed. Unsupported constraints fail
registration instead of silently weakening validation.

A result may be a string, a JSON-compatible value, or a dictionary containing
`ok`, `output`, `error`, and `ui`. Returned text is bounded to 64,000 characters.
Declarative UI supports `text`, `notice`, `table`, and `progress`; it cannot
execute code or manipulate the terminal. The adapter decides how to display
these records. Worker stdout/stderr, including native writes, are discarded;
return deliberate output instead of printing. Exceptions are reported by a
private generic failure, without their bodies or arguments.

## Distributable package

A PyPI/Git/wheel distribution publishes exactly one entry point:

```toml
[project.entry-points."vikram.extensions"]
example = "example:register"
```

The entry-point name is the extension name. The distribution's version is the
installed version. The standalone `vikram.extension_sdk` is supplied by Vikram;
packages should not install the whole chat application as a dependency.

## Interpreter and packaging

The macOS bundle includes CPython 3.13.15 from the 20260901
[python-build-standalone release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901).
The exact archive and SHA-256 are recorded in
[`scripts/third-party/extension-python.json`](../scripts/third-party/extension-python.json).
The build requires the core and extension Python versions to match exactly.
It downloads only that pinned archive, validates its digest, safely extracts its
installation and declared component licenses, signs nested binaries, and checks
interpreter and worker startup. Runtime and notices live under
`Contents/Resources/extension-python`; worker sources live under
`Contents/Resources/extension-worker`.

The exact vendored Python sources, including pip's MPL-licensed certifi, are
inventoried by digest and embedded as a deterministic source archive in the app's
notices. The existing release source-attachment builder copies those verified
bytes into `Vikram-VERSION-third-party-sources.tar`. Missing or modified source
files stop packaging/publication; no later package version is substituted.

The first explicit install retains a private copy of that pinned interpreter in
the extension data directory. Its virtual environments therefore survive app
updates and removal of old app versions. A frozen app never falls back to a
system Python. In a development checkout, the active development interpreter is
used for disposable environments. Runtime fetch/build steps retain the 20 GiB
disk reserve. A new runtime containing an unaccounted GPL/LGPL compiled component
fails packaging until its exact sources have been added to release sources.

The registry and environments live in the user data directory under
`extensions/`. Child environments allow only ordinary terminal/build variables;
provider API keys, proxy authorization, and ambient tokens are not inherited.
This does not prevent trusted extension code from reading files or making network
requests with normal host privileges. No extension prompts, results, or exception
bodies are logged by the manager or worker.

The supported lifecycle events are `before_reply` (`model`, `prompt`) and
`after_reply` (`model`, `text`, `usage`). Hooks run only for enabled extensions in
normal interactive mode. Returned declarations appear with tool activity; hooks
cannot supply system instructions or receive thinking traces.
