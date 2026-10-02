from collections import defaultdict, deque


class TaskExecutionError(Exception):
    """Raised when a task function fails during TaskGraph.run().

    Carries the failing task's name and keeps the original exception
    as its cause (__cause__).
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
        self.deps = {}
        self._state = {}

    def add(self, name, fn, depends=()):
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(depends)

    def _validate(self):
        # Reference check happens before cycle detection so that an
        # unregistered dependency is never misreported as a cycle.
        # The first reported pair is the smallest (task, dependency)
        # pair in ascending order.
        for task in sorted(self.deps):
            for dep in sorted(self.deps[task]):
                if dep not in self.tasks:
                    raise KeyError(
                        "task %r depends on unregistered task %r"
                        % (task, dep)
                    )

    def order(self):
        self._validate()
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
        """Return an independent snapshot of the most recent run, in
        stable topological order.

        Each entry holds a status (pending/running/completed/failed)
        plus the result or error details; mutating the returned object
        (including the error details mapping) does not affect the
        graph's internal state.
        """
        snapshot = {}
        for name, record in self._state.items():
            error = record["error"]
            snapshot[name] = {
                "status": record["status"],
                "result": record["result"],
                "error": dict(error) if error is not None else None,
            }
        return snapshot

    def run(self):
        # Validate before touching any state so a failed validation
        # neither executes tasks nor replaces a previous run's snapshot.
        order = self.order()
        # Fresh snapshot per run: no records leak from previous runs.
        state = {
            name: {"status": "pending", "result": None, "error": None}
            for name in order
        }
        self._state = state
        results = {}
        for name in order:
            # A fresh mapping per call containing only this task's
            # direct dependencies, with keys in ascending name order.
            inputs = {dep: results[dep] for dep in sorted(self.deps[name])}
            state[name]["status"] = "running"
            try:
                value = self.tasks[name](inputs)
            except Exception as exc:
                state[name]["status"] = "failed"
                state[name]["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                raise TaskExecutionError(name, exc) from exc
            state[name]["status"] = "completed"
            state[name]["result"] = value
            results[name] = value
        return results
