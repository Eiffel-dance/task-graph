from collections import defaultdict, deque


class TaskExecutionError(Exception):
    """Raised when a task fails during TaskGraph.run.

    Carries the failed task's name and keeps the original exception
    as __cause__.
    """

    def __init__(self, task_name, original):
        self.task_name = task_name
        self.original = original
        super().__init__(
            "task %r failed: %s: %s"
            % (task_name, type(original).__name__, original)
        )


class TaskGraph:
    def __init__(self):
        self.tasks = {}
        self.deps = defaultdict(set)
        self._snapshot = {}

    def add(self, name, fn, depends=()):
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(depends)

    def order(self):
        deps = {k: set(v) for k, v in self.deps.items()}
        out = []
        q = deque(sorted(k for k, v in deps.items() if not v))
        while q:
            n = q.popleft()
            out.append(n)
            for child in sorted(deps):
                if n in deps[child]:
                    deps[child].remove(n)
                    if not deps[child]:
                        q.append(child)
        if len(out) != len(deps):
            raise ValueError("cycle detected")
        return out

    def execution_state(self):
        """Return an independent snapshot of the last run's state.

        Mutating the returned object does not affect the graph.
        """
        return {
            name: dict(entry)
            for name, entry in self._snapshot.items()
        }

    def _new_snapshot(self, names):
        return {
            name: {"status": "pending", "result": None, "error": None}
            for name in names
        }

    def run(self):
        results = {}
        self._snapshot = self._new_snapshot(self.order())
        for name in self._snapshot:
            entry = self._snapshot[name]
            entry["status"] = "running"
            try:
                value = self.tasks[name](results)
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                raise TaskExecutionError(name, exc) from exc
            entry["status"] = "completed"
            entry["result"] = value
            results[name] = value
        return results
