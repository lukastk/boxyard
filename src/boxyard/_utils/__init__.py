def __getattr__(name):
    import importlib

    _modules = [".base", ".locking", ".rclone"]
    for mod_path in _modules:
        mod = importlib.import_module(mod_path, __name__)
        if hasattr(mod, name):
            globals()[name] = getattr(mod, name)
            return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
