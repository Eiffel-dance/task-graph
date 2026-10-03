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
        # Validate everything before mutating anything: a rejected
        # registration must leave tasks, deps and execution_state untouched.
        if not isinstance(name, str):
            raise TypeError("task name must be a string")
        if not name:
            raise ValueError("task name must not be empty")
        if not callable(fn):
            raise TypeError("task function must be callable")
        # Strings and bytes are iterable but are never a collection of
        # dependency names, so reject them explicitly.
        if isinstance(depends, (str, bytes)):
            raise TypeError("depends must be an iterable of task names")
        try:
            dep_names = list(depends)
        except TypeError:
            raise TypeError(
                "depends must be an iterable of task names"
            ) from None
        for dep in dep_names:
            if not isinstance(dep, str):
                raise TypeError("dependency name must be a string")
            if not dep:
                raise ValueError("dependency name must not be empty")
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(dep_names)

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

    def _normalize_targets(self, targets):
        # Validate a targets argument completely before ordering, execution
        # or state replacement. The input boundary mirrors add()'s depends:
        # strings/bytes and other non-iterables are never a collection of
        # task names, and repeated names collapse to a single entry.
        if isinstance(targets, (str, bytes)):
            raise TypeError("targets must be an iterable of task names")
        try:
            target_names = list(targets)
        except TypeError:
            raise TypeError(
                "targets must be an iterable of task names"
            ) from None
        for name in target_names:
            if not isinstance(name, str):
                raise TypeError("target name must be a string")
            if not name:
                raise ValueError("target name must not be empty")
        if not target_names:
            raise ValueError("targets must not be empty")
        selected = set(target_names)
        # Report an unknown target deterministically when several are given.
        unknown = sorted(name for name in selected if name not in self.tasks)
        if unknown:
            raise KeyError("unknown target task %r" % unknown[0])
        return selected

    def run(self, max_retries=0, continue_on_error=False, *, targets=None):
        # max_retries is the number of extra attempts granted to each task
        # after its first failure; zero (the default) keeps the historical
        # single-call behavior. Booleans are rejected even though they are
        # ints, since True/False is never a meaningful retry count.
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ValueError("max_retries must be a non-negative integer")
        if max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        # continue_on_error is opt-in and strictly boolean; anything else
        # is rejected before ordering, execution or state replacement so a
        # previous run's snapshot is left untouched.
        if not isinstance(continue_on_error, bool):
            raise TypeError("continue_on_error must be a boolean")
        # targets is keyword-only so the positional interpretation of the
        # historical arguments never changes. None (the default) selects the
        # whole graph; anything else must pass full input validation here,
        # before ordering and before replacing the previous snapshot.
        selected = None
        if targets is not None:
            selected = self._normalize_targets(targets)
        # Always validate the complete graph, including unselected nodes:
        # a bad node outside the requested closure must not be silently
        # accepted. order() also supplies the stable topological order whose
        # projection determines actual execution order.
        order = self.order()
        if selected is None:
            run_order = order
        else:
            # Close the selected targets over their transitive dependencies,
            # then project the full stable topological order onto that
            # closure; every direct dependency of a closure node is itself
            # in the closure, so inputs are identical to a whole-graph run.
            closure = set()
            stack = list(selected)
            while stack:
                name = stack.pop()
                if name not in closure:
                    closure.add(name)
                    stack.extend(self.deps[name])
            run_order = [name for name in order if name in closure]
        # Fresh snapshot per run covering exactly the executed closure:
        # no records leak from previous runs or from unselected nodes.
        state = {
            name: {"status": "pending", "result": None, "error": None}
            for name in run_order
        }
        self._state = state
        results = {}
        # "dead" nodes are failures plus everything that can no longer run
        # because it (transitively) depends on one. The run still advances
        # strictly in stable topological order.
        dead = set()
        failures = []
        failure_errors = {}
        for name in run_order:
            # Any direct dependency that failed or was skipped marks this
            # node as unreachable; it is never called and keeps the empty
            # pending placeholder (result/error both None).
            if self.deps[name] & dead:
                dead.add(name)
                continue
            state[name]["status"] = "running"
            # Retry budgets are per task: every task gets its own
            # max_retries extra attempts regardless of upstream outcomes.
            for attempt in range(max_retries + 1):
                # Each attempt receives a brand-new mapping containing only
                # its declared upstream values; ordering keys keeps calls
                # reproducible.
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                try:
                    value = self.tasks[name](inputs)
                except Exception as exc:
                    if attempt < max_retries:
                        continue  # retry the same task immediately
                    state[name]["status"] = "failed"
                    state[name]["error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                    if not continue_on_error:
                        raise TaskExecutionError(name, exc) from exc
                    # Record the failure immediately and press on with
                    # tasks that do not depend on it.
                    dead.add(name)
                    failures.append(name)
                    failure_errors[name] = exc
                    break
                state[name]["status"] = "completed"
                state[name]["result"] = value
                results[name] = value
                break
        if failures:
            # Report the earliest failure in stable topological order; its
            # original and __cause__ are that node's final-attempt exception.
            first = failures[0]
            original = failure_errors[first]
            raise TaskExecutionError(first, original) from original
        return results
