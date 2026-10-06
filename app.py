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


class TaskResumeError(Exception):
    """Raised when run(resume=True) cannot resume from the last snapshot.

    reason is one of:
      - "no_previous_run": no snapshot from a previous run exists;
      - "graph_changed": the task set, a task's dependencies or a
        task's priority differ from when the snapshot was taken;
      - "scope_changed": the normalized targets closure differs from
        the scope the snapshot was taken for.

    The previous snapshot is always preserved when this is raised.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__("cannot resume run: %s" % reason)


class TaskCycleError(ValueError):
    """Raised when order(), plan() or run() validate a graph with a cycle.

    cycle is a non-empty list of task names tracing the actual loop
    along "task -> direct dependency" edges, with the first node
    repeated at the end; a task depending on itself is therefore
    [name, name]. Detection is fully deterministic — roots and direct
    dependencies are visited in ascending task-name order, and only
    the first loop closed by an edge back to the current DFS path is
    reported, sliced from the first occurrence of its first node so no
    unrelated nodes tag along. Registration order, the targets
    container and every run() parameter leave both the exception type
    and this path unchanged. As a ValueError subclass it keeps the
    historical cycle exception type; the same path is also embedded in
    the message.
    """

    def __init__(self, cycle):
        self.cycle = list(cycle)
        super().__init__(
            "cycle detected: %s" % " -> ".join(self.cycle)
        )


class TaskResourceError(Exception):
    """Raised before a run when a task's declared resource demand can
    never fit the supplied resource_limits.

    No task is called and no execution_state snapshot or execution_trace
    is created or replaced: the feasibility check runs after input and
    graph validation but before ordering results are used, so the
    previous snapshot and trace survive verbatim. task_name names the
    offending task (in priority topological order, deterministically),
    resource is the resource whose limit is too small, requested is the
    task's declared demand and limit is the configured ceiling.
    """

    def __init__(self, task_name, resource, requested, limit):
        self.task_name = task_name
        self.resource = resource
        self.requested = requested
        self.limit = limit
        super().__init__(
            "task %r requests %d units of resource %r but its limit is %d"
            % (task_name, requested, resource, limit)
        )


class TaskGraph:
    def __init__(self):
        self.tasks = {}
        self.deps = defaultdict(set)
        self.priorities = {}
        # Per-task declared resource demands: task name -> mapping of
        # non-empty resource key to the positive integer units consumed
        # by one execution. Tasks registered without resources simply
        # have no entry; it is part of the graph's structural identity,
        # so a changed demand makes resume=True reject an old snapshot.
        self.resources = {}
        self._state = {}
        # Audit trail of the run that established the most recent
        # execution_state snapshot: a priority-topological-ordered list of
        # per-task records (task_name, status, attempts, errors, error).
        # None until a run has established a snapshot; validation failures
        # never touch it. Kept strictly in lockstep with self._state.
        self._trace = None
        # Whole-graph structural fingerprint captured alongside every
        # snapshot: the set of (name, sorted dependencies, priority)
        # triples for every registered task. resume=True compares a
        # fresh fingerprint to this one (and the snapshot's scope)
        # before reusing anything, so a structural or targets-closure
        # change is rejected while the old snapshot stays in place.
        self._state_fingerprint = None

    def add(self, name, fn, depends=(), *, priority=0, resources=None):
        # Validate everything before mutating anything: a rejected
        # registration must leave tasks, deps, priorities, resources and
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
        # priority is keyword-only, so the positional interpretation of
        # the historical arguments never changes. It orders ready tasks
        # (larger value first, name ascending on ties) but never overrides
        # dependencies. Booleans are rejected even though they are ints;
        # negatives are allowed and the omitted default is 0.
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError("priority must be an integer")
        # resources is keyword-only, so the positional interpretation of
        # the historical arguments never changes. None (the default) is
        # an empty demand mapping; anything else must be a mapping of
        # non-empty resource names to positive integers (booleans are
        # rejected even though they are ints). Fully validated here,
        # alongside the duplicate check, before any graph structure is
        # touched, so a rejected registration leaves no resource record.
        declared = self._normalize_resources_declaration(resources)
        if name in self.tasks:
            raise ValueError("duplicate task")
        self.tasks[name] = fn
        self.deps[name] = set(dep_names)
        self.priorities[name] = priority
        self.resources[name] = declared

    @staticmethod
    def _normalize_resources_declaration(resources):
        # Shared validation for add()'s resources: None means no demand.
        # Returns a fresh dict so later caller mutation can never reach
        # the graph. Every error is raised before any registration change.
        if resources is None:
            return {}
        if not isinstance(resources, Mapping):
            raise TypeError(
                "resources must be a mapping of resource names to "
                "positive integers"
            )
        declared = {}
        for key, amount in resources.items():
            if not isinstance(key, str):
                raise TypeError("resource name must be a string")
            if not key:
                raise ValueError("resource name must not be empty")
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise TypeError(
                    "resource demand must be a positive integer"
                )
            if amount <= 0:
                raise ValueError(
                    "resource demand must be a positive integer"
                )
            declared[key] = amount
        return declared

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

    def _find_cycle(self):
        # Deterministic first cycle over "task -> direct dependency"
        # edges. Three-color DFS: start nodes are picked in ascending
        # task-name order and each node's direct dependencies are
        # descended into in ascending name order, so the outcome depends
        # only on names and edges, never on registration order or dict
        # iteration. The first edge that closes back onto a node on the
        # current DFS path wins; the reported loop is sliced from that
        # node's first position on the path and repeats it at the end,
        # carrying no node outside the loop. A self-edge therefore comes
        # back as [name, name]. Returns None for an acyclic graph; this
        # runs only after _check_dependencies(), so every edge resolves.
        # The traversal keeps an explicit stack (rather than Python call
        # frames) so a long acyclic chain cannot trip the recursion limit.
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self.tasks}
        for root in sorted(self.tasks):
            if color[root] != WHITE:
                continue
            color[root] = GRAY
            path = [root]
            position = {root: 0}
            stack = [(root, iter(sorted(self.deps[root])))]
            while stack:
                node, deps = stack[-1]
                descended = False
                for dep in deps:
                    if color[dep] == GRAY:
                        return path[position[dep]:] + [dep]
                    if color[dep] == WHITE:
                        color[dep] = GRAY
                        position[dep] = len(path)
                        path.append(dep)
                        stack.append((dep, iter(sorted(self.deps[dep]))))
                        descended = True
                        break
                    # BLACK dependencies are already fully explored.
                if descended:
                    continue
                stack.pop()
                path.pop()
                del position[node]
                color[node] = BLACK
        return None

    def order(self):
        # Priority-aware Kahn traversal. Nodes enter the ready queue once
        # their direct dependencies are all satisfied; the dependency-free
        # roots form the first intake and every emission lets the nodes it
        # unblocks form the next one. Each intake is enqueued in
        # (-priority, name) order, so equal-priority siblings are queued by
        # ascending name. Selection takes the queued node with the largest
        # priority, breaking ties by queue position (earliest intake first).
        # That queue-position tie-break is exactly the historical FIFO
        # rule, so with every priority equal to 0 (the default) the result
        # is the pre-priority stable sequence, name for name; priorities
        # only promote ready nodes ahead of lower-priority ones, and a
        # dependency is always emitted before its successors.
        #
        # Missing dependencies keep their historical KeyError and are
        # checked first; only a fully resolved graph is cycle-checked, via
        # the deterministic DFS that raises TaskCycleError carrying the
        # first closed path. Both checks are read-only and run before any
        # ordering result is produced.
        self._check_dependencies()
        cycle = self._find_cycle()
        if cycle is not None:
            raise TaskCycleError(cycle)
        deps = {k: set(v) for k, v in self.deps.items()}
        remaining = set(deps)
        ready = []  # (priority, intake_position, name)
        position = 0

        def enqueue(names):
            nonlocal position
            ordered = sorted(
                names, key=lambda n: (-self.priorities[n], n)
            )
            for n in ordered:
                ready.append((self.priorities[n], position, n))
                position += 1

        enqueue([n for n in deps if not deps[n]])
        out = []
        while ready:
            idx = min(
                range(len(ready)),
                key=lambda i: (-ready[i][0], ready[i][1]),
            )
            _, _, n = ready.pop(idx)
            out.append(n)
            remaining.remove(n)
            newly_ready = []
            for child in remaining:
                if n in deps[child]:
                    deps[child].remove(n)
                    if not deps[child]:
                        newly_ready.append(child)
            enqueue(newly_ready)
        return out

    def execution_state(self):
        """Return an independent snapshot of the most recent run, in
        priority topological order.

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

    def execution_trace(self):
        """Return a fresh list auditing the run behind the latest snapshot.

        Entries follow that run's priority topological order, one per task
        in its executed closure, each with task_name, status
        (completed/failed/blocked/cancelled/reused/pending), attempts (the
        actual number of calls made this run), errors (type/message of every
        failed attempt, in call order) and error (the final exception
        summary, or None). Before any run has established a snapshot the
        result is an empty list; plan(), order(), add() and failed
        validation never create or replace a trace. The returned list is a
        brand-new deep copy, so mutating it never affects later queries.
        """
        if self._trace is None:
            return []
        # The trace mapping is built in run order, so iterating its values
        # yields the priority topological sequence.
        return [self._copy_trace_record(record)
                for record in self._trace.values()]

    @staticmethod
    def _copy_trace_record(record):
        entry = {
            "task_name": record["task_name"],
            "status": record["status"],
            "attempts": record["attempts"],
            "errors": [dict(err) for err in record["errors"]],
            "error": None if record["error"] is None else dict(
                record["error"]
            ),
        }
        return entry

    @staticmethod
    def _final_error(exc):
        return {"type": type(exc).__name__, "message": str(exc)}

    def _new_trace(self, run_order, reused):
        # Build the audit records for a run that is establishing a fresh
        # snapshot. Reused completed nodes are settled up front as "reused"
        # with zero calls; every other node starts "pending" with zero
        # attempts and is finalized according to how the run settles it.
        trace = {}
        for name in run_order:
            trace[name] = {
                "task_name": name,
                "status": "reused" if name in reused else "pending",
                "attempts": 0,
                "errors": [],
                "error": None,
            }
        return trace

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

    def _normalize_resource_limits(self, resource_limits):
        # Validate a resource_limits mapping completely before ordering,
        # execution or state replacement. Keys name resources any task
        # has declared (an unknown one is a KeyError); values are
        # positive integer ceilings, booleans rejected even though they
        # are ints. A declared resource absent from the mapping stays
        # unlimited, so only the named resources are bounded. Returns a
        # fresh plain dict used verbatim by the scheduler.
        if not isinstance(resource_limits, Mapping):
            raise TypeError(
                "resource_limits must be a mapping of resource names to "
                "positive integers"
            )
        ceilings = {}
        for key, ceiling in resource_limits.items():
            if not isinstance(key, str):
                raise TypeError("resource_limits key must be a string")
            if isinstance(ceiling, bool) or not isinstance(ceiling, int):
                raise TypeError(
                    "resource limit must be a positive integer"
                )
            if ceiling <= 0:
                raise ValueError(
                    "resource limit must be a positive integer"
                )
            ceilings[key] = ceiling
        declared_resources = set()
        for demands in self.resources.values():
            declared_resources.update(demands)
        # Report an unknown resource deterministically when several are
        # given; empty keys are covered here as well (no task can
        # declare one), surfacing as the same KeyError.
        unknown = sorted(
            key for key in ceilings if key not in declared_resources
        )
        if unknown:
            raise KeyError(
                "unknown resource %r in resource_limits" % unknown[0]
            )
        return ceilings

    def _check_resource_feasibility(self, scope_order, ceilings):
        # Reject a demand that can never fit: the task asks for more
        # units of a bounded resource than the run will ever make
        # available, so admitting it is impossible regardless of
        # scheduling. Runs entirely before any task is called and before
        # snapshot/trace replacement. scope_order is the priority
        # topological sequence of tasks that may actually execute this
        # run (reused nodes excluded), and the first offender there wins
        # for determinism, with its offending resource sorted by name.
        for name in scope_order:
            demands = self.resources.get(name, {})
            for resource in sorted(demands):
                requested = demands[resource]
                if resource in ceilings and requested > ceilings[resource]:
                    raise TaskResourceError(
                        name, resource, requested, ceilings[resource]
                    )

    def plan(self, targets=None):
        """Read-only preview of the priority task sequence run() would cover.

        targets=None (the default) covers the whole graph; an iterable of
        task names is deduplicated and closed over transitive dependencies,
        and the result is the projection of the full priority topological
        order onto that closure. Validation mirrors run(): bad targets
        raise TypeError/ValueError/KeyError, missing dependencies raise
        KeyError and any cycle anywhere in the graph raises
        TaskCycleError (a ValueError subclass) carrying its deterministic
        cycle path — all before any ordering result is produced. No task function is called, no
        execution_state snapshot is created or replaced, and the graph is
        never mutated. Each successful call returns a new list, so callers
        may freely mutate the result.
        """
        return self._resolve_run_order(targets)

    def run(self, max_retries=0, continue_on_error=False, *, targets=None,
            max_concurrency=1, retry_limits=None, cancel_check=None,
            resume=False, resource_limits=None):
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
        # resume is keyword-only, so the positional interpretation of the
        # historical arguments never changes. False (the default) keeps the
        # historical fresh-run semantics exactly: a brand-new snapshot is
        # taken and every task is called. A strictly boolean value is
        # required; anything else is rejected before ordering, execution or
        # state replacement so the previous snapshot is left untouched.
        if not isinstance(resume, bool):
            raise TypeError("resume must be a boolean")
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
        # resource_limits is keyword-only, so the positional
        # interpretation of the historical arguments never changes.
        # None (the default) places no ceiling on any declared resource
        # and keeps every old result and ordering exactly; a mapping
        # names the resources that are bounded — declared resources it
        # omits stay unlimited. Full validation happens here, before
        # ordering, execution and replacement of the previous snapshot,
        # so a rejected mapping leaves the last execution_state and
        # execution_trace untouched.
        resource_constraints = None
        if resource_limits is not None:
            resource_constraints = self._normalize_resource_limits(
                resource_limits
            )
        # targets is keyword-only so the positional interpretation of the
        # historical arguments never changes. None (the default) selects the
        # whole graph; anything else must pass full input validation here,
        # before ordering and before replacing the previous snapshot.
        # _resolve_run_order validates targets, validates the complete
        # graph (including unselected nodes) and returns the priority
        # topological sequence this run covers — the exact sequence plan()
        # would preview for the same graph and targets.
        run_order = self._resolve_run_order(targets)
        reused = set()
        if resume:
            # Validate the resumability of the existing snapshot fully
            # before any task is called or the snapshot is replaced:
            # no snapshot at all (no_previous_run), a structural change
            # (graph_changed) or a different targets closure
            # (scope_changed) raise TaskResumeError and leave the old
            # snapshot exactly in place. Only "completed" records are
            # reusable; failed/pending/cancelled nodes are re-executed.
            reused = self._prepare_resume(run_order)
            if len(reused) == len(run_order):
                # Everything in scope already completed: no task is
                # called, results come back in priority topological
                # order, and the previous snapshot is retained as-is.
                return {
                    name: self._state[name]["result"] for name in run_order
                }
        if resource_constraints is not None:
            # A declared demand above its limit can never be admitted by
            # any batch, so it is rejected before any task runs and
            # before the snapshot/trace are replaced. Reused completed
            # tasks are not called again and spend no resource units, so
            # only this run's executing closure is considered; the
            # offending task is picked in priority topological order and
            # its resource in sorted-key order, deterministically.
            self._check_resource_feasibility(
                [name for name in run_order if name not in reused],
                resource_constraints,
            )
        # Fresh snapshot for the work this invocation performs. Reused
        # nodes keep a completed record carrying their old result; every
        # other node starts from an empty pending placeholder, so old
        # failed/cancelled result/error values never linger. The
        # snapshot covers exactly the executed closure.
        state = {}
        for name in run_order:
            if name in reused:
                state[name] = {
                    "status": "completed",
                    "result": self._state[name]["result"],
                    "error": None,
                }
            else:
                state[name] = {
                    "status": "pending", "result": None, "error": None
                }
        # The audit trail covers the same closure and is established
        # together with the snapshot: reused nodes settle as "reused"
        # without a single call, every other node starts "pending" and is
        # finalized as the scheduler reaches (or skips) it.
        trace = self._new_trace(run_order, reused)
        self._state = state
        self._trace = trace
        self._state_fingerprint = self._fingerprint()
        if max_concurrency > 1:
            return self._run_concurrent(
                run_order, state, trace, max_retries, continue_on_error,
                max_concurrency, limits, cancel_check, reused,
                resource_constraints,
            )
        results = {
            name: state[name]["result"]
            for name in run_order if name in reused
        }
        # "dead" nodes are failures plus everything that can no longer run
        # because it (transitively) depends on one. The run still advances
        # strictly in priority topological order.
        dead = set()
        failures = []
        failure_errors = {}
        for name in run_order:
            # Reused successes are settled output of a previous run: they
            # are never called again, spend no concurrency slot and are
            # neither polled for cancellation nor eligible for "dead".
            if name in reused:
                continue
            # Any direct dependency that failed or was skipped marks this
            # node as unreachable; it is never called and keeps the empty
            # pending placeholder (result/error both None).
            if self.deps[name] & dead:
                dead.add(name)
                trace[name]["status"] = "blocked"
                continue
            # Cooperative cancellation is consulted exactly once per task
            # that (re)starts this run, in priority topological order,
            # immediately before that task starts — never for reused
            # nodes or unreachable nodes and never between retries. A
            # truthy result spends none of this task's retries; a raising
            # callback aborts as TaskControlError.
            if cancel_check is not None:
                if self._poll_cancel(
                    cancel_check, run_order, state, trace
                ):
                    cancelled = self._mark_cancelled(
                        run_order, state, trace
                    )
                    raise TaskCancelledError(cancelled)
            state[name]["status"] = "running"
            # Retry budgets are per task and counted afresh for this
            # recovery: a retry_limits entry overrides max_retries for
            # this task alone, and attempts spent in the previous run do
            # not carry over.
            record = trace[name]
            budget = limits.get(name, max_retries)
            for attempt in range(budget + 1):
                # Each attempt receives a brand-new mapping containing
                # only its declared upstream values (reused or freshly
                # computed); ordering keys keeps calls reproducible.
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                record["attempts"] += 1
                try:
                    value = self.tasks[name](inputs)
                except Exception as exc:
                    record["errors"].append(self._final_error(exc))
                    if attempt < budget:
                        continue  # retry the same task immediately
                    state[name]["status"] = "failed"
                    state[name]["error"] = self._final_error(exc)
                    record["status"] = "failed"
                    record["error"] = self._final_error(exc)
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
                record["status"] = "completed"
                results[name] = value
                break
        if failures:
            # Report the earliest failure in priority topological order; its
            # original and __cause__ are that node's final-attempt exception.
            first = failures[0]
            original = failure_errors[first]
            raise TaskExecutionError(first, original) from original
        # Reused entries were seeded before any new completion, so order
        # the returned mapping by the priority topological sequence.
        return {name: results[name] for name in run_order}

    def _fingerprint(self):
        # Structural identity of the complete graph at snapshot time:
        # every registered task's name, direct dependencies
        # (order-normalized), priority and declared resource demands
        # (key-order-normalized, so demand is part of the graph's
        # identity — resource_limits itself is a run parameter and is
        # not fingerprinted). The fingerprint deliberately covers the
        # whole graph (not just one targets closure) so that adding,
        # removing, re-wiring or re-declaring any task — inside or
        # outside the resumed closure — is a graph change, while a
        # different targets closure on an untouched graph is a scope
        # change.
        return frozenset(
            (
                name,
                tuple(sorted(self.deps[name])),
                self.priorities[name],
                tuple(sorted(self.resources.get(name, {}).items())),
            )
            for name in self.tasks
        )

    def _prepare_resume(self, run_order):
        # Verify resume=True against the most recent snapshot without
        # calling any task or replacing the snapshot. Returns the set of
        # nodes whose completed result may be reused.
        if self._state_fingerprint is None:
            raise TaskResumeError("no_previous_run")
        # Structural identity is checked first, then scope: a changed
        # task name set, dependency set or priority is graph_changed
        # even when the requested closure also differs; an untouched
        # graph whose normalized targets closure is a different member
        # set is scope_changed.
        if self._fingerprint() != self._state_fingerprint:
            raise TaskResumeError("graph_changed")
        if set(run_order) != set(self._state):
            raise TaskResumeError("scope_changed")
        return {
            name
            for name in run_order
            if self._state[name]["status"] == "completed"
        }

    def _poll_cancel(self, cancel_check, run_order, state, trace=None):
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
            self._mark_cancelled(run_order, state, trace)
            raise TaskControlError(exc) from exc
        return requested

    @staticmethod
    def _mark_cancelled(run_order, state, trace=None):
        # Flip every record that never started to "cancelled", walking
        # the priority topological order so the returned names are stable
        # too. Completed/failed records (with results or final errors)
        # are left exactly as they settled. The audit trail is finalized
        # the same way when one is being kept.
        cancelled = []
        for name in run_order:
            if state[name]["status"] == "pending":
                state[name]["status"] = "cancelled"
                cancelled.append(name)
                if trace is not None:
                    trace[name]["status"] = "cancelled"
        return cancelled

    def _run_concurrent(self, run_order, state, trace, max_retries,
                        continue_on_error, max_concurrency, retry_limits,
                        cancel_check=None, reused=frozenset(),
                        resource_ceilings=None):
        """Batch scheduler used when max_concurrency > 1.

        Each round starts at most max_concurrency ready tasks — those whose
        direct dependencies all completed successfully — and waits for the
        whole batch to settle before the next round. run_order already is
        the priority topological order (largest priority among the ready
        nodes, the historical name-ordered FIFO intake on ties), and
        pending always stays a prefix-filtered view of it, so the ready
        nodes are walked in exactly the order order() would start them;
        nothing about the batching re-sorts them. Everything observable
        (input mappings, results key order, failure selection, state
        records) follows that priority topological order, never the order
        in which concurrent tasks happen to finish.

        When resource_ceilings is a mapping, a ready node joins the batch
        only while a concurrency slot is free and the batch's cumulative
        declared demand for every bounded resource stays within its
        ceiling. The first ready node that does not fit ends the round —
        the walk never skips past it to a later ready node — and it is
        reconsidered only after the whole batch (retries included) has
        settled and released every unit it held. The per-run feasibility
        check already proved each individual demand fits its ceiling, so
        the first ready node always admits into an empty batch and the
        scheduler can never deadlock waiting for units; resources absent
        from the mapping (or a None mapping) stay unlimited and the
        behavior is exactly the historical batching.

        On a resumed run, reused nodes are already completed in state:
        their results seed the mapping below, they never enter pending or
        a batch and therefore never occupy a concurrency slot or any
        resource units.
        """
        position = {name: i for i, name in enumerate(run_order)}
        results = {
            name: state[name]["result"]
            for name in run_order if name in reused
        }
        # "dead" nodes are failures plus everything that can no longer run
        # because it (transitively) depends on one.
        dead = set()
        failures = []
        failure_errors = {}
        pending = [name for name in run_order if name not in reused]

        def attempt(name):
            # Retry budgets are per task — a retry_limits entry overrides
            # max_retries for this task alone — and every attempt receives
            # a brand-new mapping of its direct dependencies, exactly as in
            # the sequential scheduler. results is only written by the
            # scheduler thread between batches, so reads here are safe. The
            # audit list is appended only by this task's own worker, so no
            # synchronization is needed; it is read back on the scheduler
            # thread after the whole batch has settled.
            errors = []
            last = None
            calls = 0
            for _ in range(retry_limits.get(name, max_retries) + 1):
                inputs = {d: results[d] for d in sorted(self.deps[name])}
                calls += 1
                try:
                    return True, self.tasks[name](inputs), None, calls, errors
                except Exception as exc:
                    errors.append(self._final_error(exc))
                    last = exc
            return False, None, last, calls, errors

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
                        trace[name]["status"] = "blocked"
                    elif all(d in results for d in self.deps[name]):
                        ready.append(name)
                if not ready:
                    break  # only unreachable nodes remain
                # pending stays in run_order (priority topological order),
                # so the ready nodes are walked in precisely the order
                # order() would start them — no re-sorting here.
                # Cooperative cancellation is consulted exactly once per
                # batch, in priority topological order, after the previous
                # batch has fully settled and before any node of this one
                # starts — so a cancelled node never runs and never spends
                # a retry. A truthy result still waits for nothing here
                # (no batch is in flight between rounds) and leaves every
                # already-submitted batch recorded, because rounds only
                # advance once a whole batch has settled.
                if cancel_check is not None:
                    if self._poll_cancel(
                        cancel_check, run_order, state, trace
                    ):
                        cancelled = self._mark_cancelled(
                            run_order, state, trace
                        )
                        raise TaskCancelledError(cancelled)
                # Build the batch as the longest fitting prefix of the
                # ready sequence: each next node may join only while a
                # concurrency slot is free and its declared demand keeps
                # every bounded resource within its ceiling. The first
                # node that does not fit ends this round immediately —
                # later ready nodes are never skipped ahead to it — and
                # is reconsidered once the settled batch releases its
                # units. With no resource_ceilings this is exactly the
                # historical ready[:max_concurrency] prefix.
                batch = []
                used = {}
                for name in ready:
                    if len(batch) >= max_concurrency:
                        break
                    demands = self.resources.get(name, {})
                    if resource_ceilings is not None:
                        fits = True
                        for resource, amount in demands.items():
                            ceiling = resource_ceilings.get(resource)
                            if ceiling is not None and (
                                used.get(resource, 0) + amount > ceiling
                            ):
                                fits = False
                                break
                        if not fits:
                            break
                    batch.append(name)
                    for resource, amount in demands.items():
                        used[resource] = used.get(resource, 0) + amount
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
                # in completion order — including attempts and per-attempt
                # errors in the audit trail.
                for name in batch:
                    ok, value, exc, calls, errors = outcomes[name]
                    record = trace[name]
                    record["attempts"] = calls
                    record["errors"] = errors
                    if ok:
                        state[name]["status"] = "completed"
                        state[name]["result"] = value
                        record["status"] = "completed"
                        results[name] = value
                    else:
                        state[name]["status"] = "failed"
                        state[name]["error"] = self._final_error(exc)
                        record["status"] = "failed"
                        record["error"] = self._final_error(exc)
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
