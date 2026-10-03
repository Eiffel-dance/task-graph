# Task Graph

A dependency-free Python reference implementation for workflow, task-planning, scheduling.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个可校验的任务有向无环图执行器。它应支持添加带依赖的任务、检测重复节点与环、按稳定拓扑序执行，并在失败时保留已完成和失败节点的可观察状态。调度结果必须可复现，任务函数只接收明确输入，后续可在同一接口上接入并发和重试策略。

## Retry policy

`run(max_retries=n)` 为可选的按任务重试策略：`n` 表示每个任务在首次失败后允许再次调用的次数（默认 `0`，即每个任务只调用一次）。`n` 必须是非负整数（布尔值无效），非法值在任何任务执行前抛出 `ValueError`，且不影响上一轮 `execution_state()` 快照。任务抛异常时立即重试同一任务，重试预算按任务独立计算；每次尝试都收到只含直接依赖结果的全新映射。耗尽次数仍失败时抛出 `TaskExecutionError`（`task_name`、`original` 与 `__cause__` 均指向最后一次异常），下游任务不执行。

## Continue-on-error scheduling

`run(continue_on_error=True)` 为可选的非快速失败调度（默认 `False`，保持原有在第一个最终失败处停止的行为）。该参数只接受布尔值，其他类型在任何任务执行或状态替换前抛出 `TypeError`，并保留上一份 `execution_state()` 快照。开启后，任务在用尽 `max_retries` 仍失败时立即记为 `failed`，调度继续处理其余可运行的独立任务；任何直接或间接依赖失败任务的节点保持 `pending`（结果与错误字段为空值）且不会被调用。所有可运行节点处理完后，只要出现失败，仍抛出 `TaskExecutionError`，其主体为稳定拓扑序中最早的失败节点，`task_name`、`original` 与 `__cause__` 均指向该节点最后一次尝试的原始异常。`execution_state()` 同时反映已完成、失败与因依赖失败而未执行的节点；若无失败，返回映射及键顺序与默认行为一致。
