"""Selection engine — bottom-n eviction logic."""


class SelectionEngine:
    def __init__(self, runtime_mode: str, n_evict: int) -> None:
        raise NotImplementedError

    def run_selection(
        self,
        workspace_etas: dict[str, float],
    ) -> tuple[list[str], list[str]]:
        raise NotImplementedError
