import unittest
import app
from app import TaskGraph, TaskExecutionError


class SmokeTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(app)


class MissingDependencyTest(unittest.TestCase):
    def test_order_reports_task_and_missing_dependency(self):
        g = TaskGraph()
        g.add("a", lambda r: 1, depends=["ghost"])
        with self.assertRaises(KeyError) as ctx:
            g.order()
        message = ctx.exception.args[0]
        self.assertIn("a", message)
        self.assertIn("ghost", message)

    def test_first_report_ordered_by_task_then_dependency(self):
        g = TaskGraph()
        g.add("b", lambda r: 1, depends=["phantom", "ghost"])
        g.add("a", lambda r: 1, depends=["nope"])
        with self.assertRaises(KeyError) as ctx:
            g.order()
        message = ctx.exception.args[0]
        self.assertIn("a", message)
        self.assertIn("nope", message)
        self.assertNotIn("b", message)

        g2 = TaskGraph()
        g2.add("t", lambda r: None, depends=["z", "a", "m"])
        with self.assertRaises(KeyError) as ctx2:
            g2.order()
        self.assertIn(repr("t"), ctx2.exception.args[0])
        self.assertIn(repr("a"), ctx2.exception.args[0])

    def test_run_missing_dependency_executes_nothing_and_preserves_snapshot(self):
        g = TaskGraph()
        calls = []
        g.add("ok", lambda r: calls.append("ok") or 42)
        self.assertEqual(g.run(), {"ok": 42})
        g.add("later", lambda r: calls.append("later"), depends=["missing"])
        before = g.execution_state()
        with self.assertRaises(KeyError):
            g.run()
        self.assertEqual(calls, ["ok"])  # no task ran in the failed invocation
        self.assertEqual(g.execution_state(), before)
        self.assertEqual(g.execution_state()["ok"]["status"], "completed")

    def test_missing_dependency_is_not_reported_as_cycle(self):
        g = TaskGraph()
        g.add("a", lambda r: None, depends=["b"])
        g.add("b", lambda r: None, depends=["c"])
        with self.assertRaises(KeyError):
            g.order()

    def test_forward_registration_still_supported(self):
        g = TaskGraph()
        g.add("leaf", lambda r: r["root"] + 1, depends=["root"])
        g.add("root", lambda r: 41)
        self.assertEqual(g.order(), ["root", "leaf"])
        self.assertEqual(g.run(), {"root": 41, "leaf": 42})


class DuplicateAndCycleTest(unittest.TestCase):
    def test_duplicate_task(self):
        g = TaskGraph()
        g.add("x", lambda r: None)
        with self.assertRaises(ValueError) as ctx:
            g.add("x", lambda r: None)
        self.assertIn("duplicate task", str(ctx.exception))

    def test_cycle(self):
        g = TaskGraph()
        g.add("a", lambda r: None, depends=["b"])
        g.add("b", lambda r: None, depends=["a"])
        with self.assertRaises(ValueError) as ctx:
            g.order()
        self.assertIn("cycle detected", str(ctx.exception))


class InputBoundaryTest(unittest.TestCase):
    def test_only_direct_dependencies_visible_with_sorted_keys(self):
        captured = {}

        def root(r):
            captured["root"] = r
            return None  # None results are retained

        def mid(r):
            captured["mid"] = r
            self.assertEqual(list(r), ["root"])
            self.assertIsNone(r["root"])
            return [1, 2]

        def leaf(r):
            captured["leaf"] = r
            self.assertEqual(list(r), sorted(list(r)))
            self.assertEqual(set(r), {"mid", "zzz"})  # root not visible
            return "done"

        g = TaskGraph()
        g.add("root", root)
        g.add("zzz", lambda r: "z")
        g.add("mid", mid, depends=["root"])
        g.add("leaf", leaf, depends=["zzz", "mid"])
        order = g.order()
        results = g.run()
        self.assertEqual(list(results), order)
        self.assertEqual(captured["root"], {})
        self.assertIsNone(results["root"])
        self.assertEqual(results["mid"], [1, 2])
        self.assertEqual(results["leaf"], "done")

    def test_input_mutation_is_isolated(self):
        def naughty(r):
            r["injected"] = "evil"
            r.clear()
            return 1

        def consumer(r):
            self.assertEqual(r, {"a": 1})
            return 2

        g = TaskGraph()
        g.add("a", naughty)
        g.add("b", consumer, depends=["a"])
        self.assertEqual(g.run(), {"a": 1, "b": 2})
        self.assertEqual(g.run(), {"a": 1, "b": 2})

    def test_results_keys_follow_stable_topological_order(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)
        g.add("c", lambda r: r["a"] + r["b"], depends=["a", "b"])
        self.assertEqual(list(g.run()), ["a", "b", "c"])


class ExecutionFailureTest(unittest.TestCase):
    def test_failure_wrapped_with_cause_and_details(self):
        g = TaskGraph()
        calls_c = []
        g.add("a", lambda r: 1)

        def boom(r):
            raise RuntimeError("boom msg")

        g.add("b", boom, depends=["a"])
        g.add("c", lambda r: calls_c.append(1) or 3, depends=["b"])

        with self.assertRaises(TaskExecutionError) as ctx:
            g.run()
        err = ctx.exception
        self.assertEqual(err.task_name, "b")
        self.assertIsInstance(err.original, RuntimeError)
        self.assertEqual(str(err.original), "boom msg")
        self.assertIs(err.__cause__, err.original)
        message = str(err)
        self.assertIn("b", message)
        self.assertIn("RuntimeError", message)
        self.assertIn("boom msg", message)
        self.assertEqual(calls_c, [])  # pending nodes are not executed

        state = g.execution_state()
        self.assertEqual(
            state["a"], {"status": "completed", "result": 1, "error": None}
        )
        self.assertEqual(state["b"]["status"], "failed")
        self.assertIsNone(state["b"]["result"])
        self.assertEqual(
            state["b"]["error"],
            {"type": "RuntimeError", "message": "boom msg"},
        )
        self.assertEqual(
            state["c"], {"status": "pending", "result": None, "error": None}
        )

    def test_snapshot_is_independent_including_error_details(self):
        g = TaskGraph()

        def fail(r):
            raise ValueError("v")

        g.add("f", fail)
        with self.assertRaises(TaskExecutionError):
            g.run()
        snapshot = g.execution_state()
        snapshot["f"]["status"] = "completed"
        snapshot["f"]["error"]["message"] = "tampered"
        snapshot["new"] = {}

        again = g.execution_state()
        self.assertEqual(again["f"]["status"], "failed")
        self.assertEqual(again["f"]["error"]["message"], "v")
        self.assertNotIn("new", again)

    def test_each_run_starts_fresh_and_reexecutes(self):
        counter = {"n": 0}

        def bump(r):
            counter["n"] += 1
            return counter["n"]

        g = TaskGraph()
        g.add("a", bump)
        g.add("b", lambda r: r["a"] * 10, depends=["a"])
        self.assertEqual(g.run(), {"a": 1, "b": 10})
        self.assertEqual(g.run(), {"a": 2, "b": 20})
        state = g.execution_state()
        self.assertTrue(
            all(record["status"] == "completed" for record in state.values())
        )

    def test_arbitrary_result_object_preserved(self):
        marker = object()
        g = TaskGraph()
        g.add("o", lambda r: marker)
        self.assertIs(g.run()["o"], marker)


if __name__ == "__main__":
    unittest.main()
