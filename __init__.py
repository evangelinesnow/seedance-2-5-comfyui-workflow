"""ComfyUI V3 entry point for the Seedance 2.5 node pack.

This pack targets the current V3 extension loader.  The node classes inherit
``comfy_api.latest.io.ComfyNode`` and are registered by ``comfy_entrypoint``;
they are intentionally not advertised as legacy V1 mappings because a V3
schema cannot be executed through the old ``INPUT_TYPES`` contract.
"""

async def comfy_entrypoint():
    """Load the extension lazily so standalone client tests need no ComfyUI install."""
    from .seedance_nodes import comfy_entrypoint as _entrypoint
    return await _entrypoint()

__all__ = ["comfy_entrypoint"]
