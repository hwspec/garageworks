import importlib
import sys

from .. import config
from .aved import AvedBackend
from .base import Backend

BACKENDS = {
    "aved": AvedBackend,
}


def resolve_backend(name, cfg):
    """
    Return a Backend instance for `name` (config.PLATFORM, or an explicit
    --platform).

    `name` is either a built-in platform (BACKENDS above) or
    'module:ClassName' for a project-local backend: the module is imported
    from config.BACKEND_DIR (./gwbackends), mirroring how testdir holds
    project-local target_bridge.py/tb_base.py.
    """
    if name in BACKENDS:
        return BACKENDS[name](cfg)

    if ":" not in name:
        raise RuntimeError(
            f"Unknown platform {name!r}. Built in: {', '.join(sorted(BACKENDS))}; "
            "or use 'module:ClassName' for a custom backend in "
            f"{config.BACKEND_DIR}/"
        )

    module_name, _, class_name = name.partition(":")
    backend_dir = str(config.BACKEND_DIR)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls(cfg)
