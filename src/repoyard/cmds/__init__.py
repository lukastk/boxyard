def __getattr__(name):
    import importlib
    _name_to_module = {
        'create_user_symlinks': '._create_user_symlinks',
        'delete_repo': '._delete_repo',
        'exclude_repo': '._exclude_repo',
        'get_repo_sync_status': '._get_repo_sync_status',
        'include_repo': '._include_repo',
        'init_repoyard': '._init_repoyard',
        'modify_repometa': '._modify_repometa',
        'new_repo': '._new_repo',
        'sync_missing_repometas': '._sync_missing_repometas',
        'sync_repo': '._sync_repo',
    }
    if name in _name_to_module:
        mod = importlib.import_module(_name_to_module[name], __name__)
        attr = getattr(mod, name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
