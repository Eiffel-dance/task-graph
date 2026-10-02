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
        self.deps = defaultdict(set)
        self._state = {}

    def add(self, name, fn, depends=()):
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(depends)

    def _check_dependencies(self):
        # Dependencies may be registered after the task that names them,
        # but by the time the graph is validated every name must resolve.
        # With several missing references, report deterministically:
        # smallest task name first, then its smallest missing dependency.
        for name in sorted(self.deps):
            missing = sorted(d for d in self.deps[name] if d not in self.tasks)
            if missing:
                raise KeyError(
                    "task %r depends on missing task %r" % (name, missing[0])
                )

    def order(self):
        self._check_dependencies()
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
        does not affect the graph's internal state.
        """
        snapshot = {}
        for name, record in self._state.items():
            entry = dict(record)
            if entry["error"] is not None:
                entry["error"] = dict(entry["error"])
            snapshot[name] = entry
        return snapshot

    def run(self, max_retries=0):
        # Validate the retry option before anything else: an invalid value
        # must abort before graph validation/task execution and must not
        # discard a previous run's snapshot. bool is a subclass of int, so
        # reject it explicitly.
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")

        # Validate before touching any state: a missing dependency must
        # neither execute tasks nor discard a previous run's snapshot.
        order = self.order()
        # Fresh snapshot per run: no records leak from previous runs.
        state = {
            name: {"status": "pending", "result": None, "error": None}
            for name in order
        }
        self._state = state
        results = {}
        for name in order:
            # The retry budget is per task: each task gets one initial call
            # plus up to max_retries immediate retries of that same task.
            last_exc = None
            value = None
            for _attempt in range(max_retries + 1):
                state[name]["status"] = "running"
                # Each attempt receives a brand-new mapping containing only
                # the task's declared upstream values; ordering keys keeps
                # calls reproducible, including retries.
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                try:
                    value = self.tasks[name](inputs)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
            if last_exc is not None:
                # Details and causal chain come from the final attempt;
                # downstream tasks never run.
                state[name]["status"] = "failed"
                state[name]["error"] = {
                    "type": type(last_exc).__name__,
                    "message": str(last_exc),
                }
                raise TaskExecutionError(name, last_exc) from last_exc
            state[name]["status"] = "completed"
            state[name]["result"] = value
            results[name] = value
        return results
