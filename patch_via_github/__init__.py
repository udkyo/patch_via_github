"""patch_via_github - Tool used to modify a local 'repo sync' with changes from GitHub pull requests."""


def __getattr__(name: str) -> str:
    if name != "__version__":
        msg = f"module {__name__} has no attribute {name}"
        raise AttributeError(msg)

    from importlib.metadata import version

    return version("patch-via-github")
