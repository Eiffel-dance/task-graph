import heapq
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed


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


class TaskCancelledError(Exception):
    """Raised when a run is stopped cooperatively via cancel_check.

    task_names lists, in priority topological order, every node of the
    run's closure that had not started when cancellation took effect;
    their snapshot records carry status "cancelled" with result and
    error both None.
    """

    def __init__(self, task_names):
        self.task_names = list(task_names)
        names = ", ".join(repr(name) for name in self.task_names)
        super().__init__("run cancelled before tasks started: %s" % names)


class TaskControlError(Exception):
    """Raised when the cancel_check callback itself raises.

    original is the callback exception and __cause__ points to it as
    well. Completed/failed snapshot records are preserved; every node
    that had not started is marked cancelled.
    """

    def __init__(self, original):
        self.original = original
        super().__init__(
            "cancel_check raised %s: %s"
            % (type(original).__name__, original)
        )


class TaskGraph:
    def __init__(self):
        self.tasks = {}
        self.deps = defaultdict(set)
        self.priorities = {}
        self._state = {}

    def add(self, name, fn, depends=(), *, priority=0):
        # Validate everything before mutating anything: a rejected
        # registration must leave tasks, deps, priorities and
        # execution_state untouched.
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
        # Priority is keyword-only and optional, defaulting to 0. It is a
        # plain integer (booleans are ints but True/False is never a
        # meaningful priority) and may be negative; larger values win.
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError("priority must be an integer")
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(dep_names)
        self.priorities[name] = priority

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
        # Priority topological order: a node is only selectable once every
        # direct dependency has been emitted, and among the ready nodes the
        # largest priority wins. Ties keep the historical stable sequence:
        # the initial ready roots and each freshly released sibling batch
        # enter the queue in ascending name order, so equal priorities emit
        # exactly the old FIFO-Kahn order (identical to the all-zero
        # default). Negative priorities are allowed and simply lose to
        # larger ones.
        self._check_dependencies()
        deps = {k: set(v) for k, v in self.deps.items()}
        out = []
        ready = []  # heap of (-priority, sequence, name)
        sequence = 0
        for name in sorted(n for n, d in deps.items() if not d):
            heapq.heappush(
                ready, (-self.priorities[name], sequence, name)
            )
            sequence += 1
        while ready:
            _, _, n = heapq.heappop(ready)
            out.append(n)
            for child in sorted(deps):
                if n in deps[child]:
                    deps[child].remove(n)
                    if not deps[child]:
                        heapq.heappush(
                            ready,
                            (-self.priorities[child], sequence, child),
                        )
                        sequence += 1
        if len(out) != len(deps):
            raise ValueError("cycle detected")
        return out

    def execution_state(self):
        """Return an independent snapshot of the most recent run, in
        priority topological order (the order() sequence).

        Each entry holds a status
        (pending/running/completed/failed/cancelled) plus the result or
        error details; mutating the returned object does not affect the
        graph's internal state.
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

    def _resolve_run_order(self, targets):
        # Shared by run() and plan(): validate targets (when given), then
        # validate the complete graph and derive the priority topological
        # sequence this invocation covers. Returns a brand-new list on
        # every call and never touches task functions or self._state.
        selected = None
        if targets is not None:
            selected = self._normalize_targets(targets)
        # Always validate the complete graph, including unselected nodes:
        # a bad node outside the requested closure must not be silently
        # accepted. order() also supplies the priority topological order whose
        # projection determines the covered sequence.
        order = self.order()
        if selected is None:
            return order
        # Close the selected targets over their transitive dependencies,
        # then project the full priority topological order onto that closure.
        closure = set()
        stack = list(selected)
        while stack:
            name = stack.pop()
            if name not in closure:
                closure.add(name)
                stack.extend(self.deps[name])
        return [name for name in order if name in closure]

    def _normalize_retry_limits(self, retry_limits):
        # Validate a retry_limits mapping completely before ordering,
        # execution or state replacement. Each entry overrides max_retries
        # for one task; tasks without an entry keep the max_retries
        # default. Booleans are rejected even though they are ints, since
        # True/False is never a meaningful retry count.
        if not isinstance(retry_limits, Mapping):
            raise TypeError(
                "retry_limits must be a mapping of task names to "
                "non-negative integers"
            )
        limits = {}
        for name, limit in retry_limits.items():
            if not isinstance(name, str):
                raise TypeError("retry_limits task name must be a string")
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("retry limit must be a non-negative integer")
            if limit < 0:
                raise ValueError(
                    "retry limit must be a non-negative integer"
                )
            limits[name] = limit
        # Report an unknown task deterministically when several are given.
        unknown = sorted(name for name in limits if name not in self.tasks)
        if unknown:
            raise KeyError("unknown task %r in retry_limits" % unknown[0])
        return limits

    def plan(self, targets=None):
        """Read-only preview of the priority task sequence run() would cover.

        targets=None (the default) covers the whole graph; an iterable of
        task names is deduplicated and closed over transitive dependencies,
        and the result is the projection of the full priority topological
        order onto that closure. Validation mirrors run(): bad targets
        raise TypeError/ValueError/KeyError, and missing dependencies or
        cycles anywhere in the graph raise KeyError/ValueError — all before
        any ordering result is produced. No task function is called, no
        execution_state snapshot is created or replaced, and the graph is
        never mutated. Each successful call returns a new list, so callers
        may freely mutate the result.
        """
        return self._resolve_run_order(targets)

    def run(self, max_retries=0, continue_on_error=False, *, targets=None,
            max_concurrency=1, retry_limits=None, cancel_check=None):
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
        # max_concurrency is keyword-only, so the positional interpretation
        # of the historical arguments never changes. The default of 1 keeps
        # the sequential scheduler; only a positive integer above 1 enables
        # concurrent batches. Booleans and non-integers are TypeError,
        # integers below 1 are ValueError, and both are rejected before any
        # task runs or the previous snapshot is replaced.
        if isinstance(max_concurrency, bool) or not isinstance(
            max_concurrency, int
        ):
            raise TypeError("max_concurrency must be a positive integer")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        # cancel_check is keyword-only, so the positional interpretation of
        # the historical arguments never changes. None (the default) leaves
        # scheduling, results, snapshot fields and failure propagation
        # exactly as before; anything else must be a zero-argument callable.
        # Rejected here, before ordering, execution and replacement of the
        # previous snapshot.
        if cancel_check is not None and not callable(cancel_check):
            raise TypeError("cancel_check must be a zero-argument callable")
        # retry_limits is keyword-only, so the positional interpretation of
        # the historical arguments never changes. None (the default) gives
        # every task the uniform max_retries budget; a mapping overrides the
        # budget per named task. Full validation happens here, before
        # ordering, execution and replacement of the previous snapshot, so
        # a rejected mapping leaves the graph and the last execution_state
        # untouched. Entries for tasks outside the targets closure are
        # legal and simply never take effect.
        limits = {}
        if retry_limits is not None:
            limits = self._normalize_retry_limits(retry_limits)
        # targets is keyword-only so the positional interpretation of the
        # historical arguments never changes. None (the default) selects the
        # whole graph; anything else must pass full input validation here,
        # before ordering and before replacing the previous snapshot.
        # _resolve_run_order validates targets, validates the complete
        # graph (including unselected nodes) and returns the priority
        # topological sequence this run covers — the exact sequence plan()
        # would preview for the same graph and targets.
        run_order = self._resolve_run_order(targets)
        # Fresh snapshot per run covering exactly the executed closure:
        # no records leak from previous runs or from unselected nodes.
        state = {
            name: {"status": "pending", "result": None, "error": None}
            for name in run_order
        }
        self._state = state
        if max_concurrency > 1:
            return self._run_concurrent(
                run_order, state, max_retries, continue_on_error,
                max_concurrency, limits, cancel_check,
            )
        results = {}
        # "dead" nodes are failures plus everything that can no longer run
        # because it (transitively) depends on one. The run still advances
        # strictly in priority topological order.
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
            # Cooperative cancellation is consulted exactly once per task,
            # in priority topological order, immediately before that task
            # starts — never for unreachable nodes and never between
            # retries. A truthy result spends none of this task's retries;
            # a raising callback aborts as TaskControlError.
            if cancel_check is not None:
                if self._poll_cancel(cancel_check, run_order, state):
                    cancelled = self._mark_cancelled(run_order, state)
                    raise TaskCancelledError(cancelled)
            state[name]["status"] = "running"
            # Retry budgets are per task: a retry_limits entry overrides
            # max_retries for this task alone, and each task's budget is
            # independent of every other task's attempts.
            budget = limits.get(name, max_retries)
            for attempt in range(budget + 1):
                # Each attempt receives a brand-new mapping containing only
                # its declared upstream values; ordering keys keeps calls
                # reproducible.
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                try:
                    value = self.tasks[name](inputs)
                except Exception as exc:
                    if attempt < budget:
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
            # Report the earliest failure in priority topological order; its
            # original and __cause__ are that node's final-attempt exception.
            first = failures[0]
            original = failure_errors[first]
            raise TaskExecutionError(first, original) from original
        return results

    def _poll_cancel(self, cancel_check, run_order, state):
        # One polling point: the callback takes no arguments and any
        # truthy value counts as a cancellation request. An exception
        # raised by the callback becomes a TaskControlError whose
        # original and __cause__ are the callback exception; it never
        # masquerades as a graph/parameter error or a task failure.
        # Either way, nodes that never started settle as "cancelled"
        # before the control exception propagates.
        try:
            requested = bool(cancel_check())
        except Exception as exc:
            self._mark_cancelled(run_order, state)
            raise TaskControlError(exc) from exc
        return requested

    @staticmethod
    def _mark_cancelled(run_order, state):
        # Flip every record that never started to "cancelled", walking
        # the priority topological order so the returned names are stable
        # too. Completed/failed records (with results or final errors)
        # are left exactly as they settled.
        cancelled = []
        for name in run_order:
            if state[name]["status"] == "pending":
                state[name]["status"] = "cancelled"
                cancelled.append(name)
        return cancelled

    def _run_concurrent(self, run_order, state, max_retries,
                        continue_on_error, max_concurrency, retry_limits,
                        cancel_check=None):
        """Batch scheduler used when max_concurrency > 1.

        Each round starts at most max_concurrency ready tasks — those whose
        direct dependencies all completed successfully — and waits for the
        whole batch to settle before the next round. Everything observable
        (input mappings, results key order, failure selection, state
        records) follows the priority topological order, never the order in
        which concurrent tasks happen to finish.
        """
        position = {name: i for i, name in enumerate(run_order)}
        results = {}
        # "dead" nodes are failures plus everything that can no longer run
        # because it (transitively) depends on one.
        dead = set()
        failures = []
        failure_errors = {}
        pending = list(run_order)

        def attempt(name):
            # Retry budgets are per task — a retry_limits entry overrides
            # max_retries for this task alone — and every attempt receives
            # a brand-new mapping of its direct dependencies, exactly as in
            # the sequential scheduler. results is only written by the
            # scheduler thread between batches, so reads here are safe.
            last = None
            for _ in range(retry_limits.get(name, max_retries) + 1):
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                try:
                    return True, self.tasks[name](inputs), None
                except Exception as exc:
                    last = exc
            return False, None, last

        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            while pending:
                ready = []
                for name in pending:
                    # A direct dependency that failed or was skipped makes
                    # this node unreachable; it is never called and keeps
                    # its pending placeholder. pending stays in priority
                    # topological order, so one pass propagates deadness
                    # through whole chains.
                    if self.deps[name] & dead:
                        dead.add(name)
                    elif all(d in results for d in self.deps[name]):
                        ready.append(name)
                if not ready:
                    break  # only unreachable nodes remain
                # Cooperative cancellation is consulted exactly once per
                # batch, in priority topological order, after the previous
                # batch has fully settled and before any node of this one
                # starts — so a cancelled node never runs and never spends
                # a retry. A truthy result still waits for nothing here
                # (no batch is in flight between rounds) and leaves every
                # already-submitted batch recorded, because rounds only
                # advance once a whole batch has settled.
                if cancel_check is not None:
                    if self._poll_cancel(cancel_check, run_order, state):
                        cancelled = self._mark_cancelled(run_order, state)
                        raise TaskCancelledError(cancelled)
                batch = ready[:max_concurrency]
                launched = set(batch)
                pending = [
                    n for n in pending if n not in launched and n not in dead
                ]
                for name in batch:
                    state[name]["status"] = "running"
                futures = {pool.submit(attempt, n): n for n in batch}
                outcomes = {}
                for future in as_completed(futures):
                    outcomes[futures[future]] = future.result()
                # The batch has fully settled before anything is recorded;
                # outcomes are applied in priority topological order, never
                # in completion order.
                for name in batch:
                    ok, value, exc = outcomes[name]
                    if ok:
                        state[name]["status"] = "completed"
                        state[name]["result"] = value
                        results[name] = value
                    else:
                        state[name]["status"] = "failed"
                        state[name]["error"] = {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                        dead.add(name)
                        failures.append(name)
                        failure_errors[name] = exc
                if failures and not continue_on_error:
                    # Fail fast: the settled batch is recorded above and no
                    # further node is started.
                    break
        if failures:
            # Report the earliest failure in priority topological order; its
            # original and __cause__ are that node's final-attempt exception.
            first = min(failures, key=position.__getitem__)
            original = failure_errors[first]
            raise TaskExecutionError(first, original) from original
        # Key order follows the priority topological order, not the order in
        # which concurrent tasks happened to finish.
        return {name: results[name] for name in run_order}
