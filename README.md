# Task Graph

A dependency-free Python reference implementation for workflow, task-planning, scheduling.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个可校验的任务有向无环图执行器。它应支持添加带依赖的任务、检测重复节点与环、按稳定拓扑序执行，并在失败时保留已完成和失败节点的可观察状态。调度结果必须可复现，任务函数只接收明确输入，后续可在同一接口上接入并发和重试策略。
