# mrmpanel plugins

Plugins live under each hosting user’s `~/plugins/<name>/`.

## Rules

- Paths cannot escape the user’s home directory (`..` and absolute paths are rejected).
- Prefer `bubblewrap` when installed; otherwise the plugin runs as the OS user with cwd inside the plugin folder.
- Each plugin should provide `run.sh`.

## Stub layout

```
/home/alice/plugins/hello/
  README.md
  run.sh
```
