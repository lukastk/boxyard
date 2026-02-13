def __getattr__(name):
    import importlib

    _name_to_module = {
        "copy_from_remote": "._copy_from_remote",
        "create_user_symlinks": "._create_user_symlinks",
        "delete_box": "._delete_box",
        "exclude_box": "._exclude_box",
        "force_push_to_remote": "._force_push_to_remote",
        "get_box_sync_status": "._get_box_sync_status",
        "include_box": "._include_box",
        "init_boxyard": "._init_boxyard",
        "modify_boxmeta": "._modify_boxmeta",
        "new_box": "._new_box",
        "rename_box": "._rename_box",
        "sync_box": "._sync_box",
        "sync_missing_boxmetas": "._sync_missing_boxmetas",
        "sync_name": "._sync_name",
    }
    if name in _name_to_module:
        mod = importlib.import_module(_name_to_module[name], __name__)
        attr = getattr(mod, name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
