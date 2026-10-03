# Task Graph

A dependency-free Python reference implementation for workflow, task-planning, scheduling.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个可校验的任务有向无环图执行器。它应支持添加带依赖的任务、检测重复节点与环、按稳定拓扑序执行，并在失败时保留已完成和失败节点的可观察状态。调度结果必须可复现，任务函数只接收明确输入，后续可在同一接口上接入并发和重试策略。

## Retry policy

`run(max_retries=n)` 为可选的按任务重试策略：`n` 表示每个任务在首次失败后允许再次调用的次数（默认 `0`，即每个任务只调用一次）。`n` 必须是非负整数（布尔值无效），非法值在任何任务执行前抛出 `ValueError`，且不影响上一轮 `execution_state()` 快照。任务抛异常时立即重试同一任务，重试预算按任务独立计算；每次尝试都收到只含直接依赖结果的全新映射。耗尽次数仍失败时抛出 `TaskExecutionError`（`task_name`、`original` 与 `__cause__` 均指向最后一次异常），下游任务不执行。

## Continue-on-error scheduling

`run(continue_on_error=True)` 开启可选的非快速失败调度（默认 `False`，既有快速失败语义完全不变）。运行仍严格按稳定拓扑序推进，每次任务调用仍只接收由直接依赖结果组成的全新映射，重试预算仍按任务独立计算。

开启后，任务耗尽 `max_retries` 仍抛异常时立即记录为 `failed`，随后继续执行尚未受影响的独立任务；任何直接或间接依赖失败任务的节点都不会被调用，保持 `pending`，其 `result` 与 `error` 均为空值（`None`）。所有可运行节点处理完后，只要存在失败仍抛出 `TaskExecutionError`，并以稳定拓扑序中最早的失败节点作为异常主体（`task_name`、`original`、`__cause__` 指向该节点最后一次尝试的原始异常）。`execution_state()` 的独立快照同时反映已完成、失败以及因依赖失败而未执行的节点；若无失败，返回映射的内容与键顺序与默认行为一致。

`continue_on_error` 只接受布尔值：传入其他类型在任何任务执行或状态替换前抛出 `TypeError`，并保留上一份 `execution_state()` 快照。

## Target selection

`run(targets=[...])` 增加一个仅限关键字传入的可迭代任务名集合（省略或传 `None` 时完全保持全图行为，位置参数解释不变）。执行范围为每个目标及其全部传递依赖，重复目标只执行一次；实际执行顺序是完整图稳定拓扑序在该闭包上的投影，范围之外的任务不会被调用，闭包内任务收到的直接依赖输入映射与全图运行完全一致，返回映射包含闭包内所有成功完成的任务且键按该投影顺序排列。

`targets` 的整体校验在排序、执行和替换最近一次状态快照之前完成：字符串、bytes 或其他不可迭代对象、集合中的非字符串名称抛出 `TypeError`；空名称或空集合抛出 `ValueError`；引用不存在的任务抛出 `KeyError`。这些输入错误均不执行任何任务并保留上一份 `execution_state()` 快照。目标模式下仍先对**整张图**完成既有的缺失依赖（`KeyError`）与环（`ValueError`）校验，未选中的坏节点不会被静默当作有效图。

失败处理沿用既有规则：`max_retries` 按任务独立计算，`continue_on_error` 下只推进不依赖失败节点的已选任务，失败节点的下游保持 `pending`，最终仍报告稳定拓扑序中最早的 `TaskExecutionError`。一次目标运行结束后 `execution_state()` 只记录本次闭包内的节点（记录字段与快照独立性不变），闭包外节点既不执行也不出现在快照中；`targets` 省略或为 `None` 时仍记录整张图。

## Concurrency

`run(max_concurrency=k)` 增加一个仅限关键字传入的并发上限（默认 `1`，此时任务调用次序、返回结果与 `execution_state()` 与既有顺序执行完全一致）。`k` 必须是正整数：布尔值或其他非整数类型抛出 `TypeError`，不大于零的整数抛出 `ValueError`；校验在任何任务执行、排序和状态快照替换之前完成，失败时保留上一份 `execution_state()` 快照。

`k > 1` 时启用分批并发调度：仍先对整张图完成既有的缺失依赖（`KeyError`）与环（`ValueError`）校验，然后按稳定拓扑序确定就绪节点（全部直接依赖已成功完成），每轮最多启动 `k` 个任务并等待本批次全部收束后才进入下一轮。任务函数仍只收到由直接依赖结果组成的全新输入映射，重试预算仍按任务独立计算；输入映射的键序、返回映射的键序以及 `execution_state()` 的记录顺序始终按稳定拓扑序确定，任务完成的先后不改变任何可观察顺序。

失败语义与顺序执行一致：`continue_on_error=False`（默认）时，若批次中有任务耗尽重试仍失败，调度器等待本批次已启动的任务收束后停止启动新节点，抛出稳定拓扑序中最早失败节点的 `TaskExecutionError`（`task_name`、`original`、`__cause__` 指向该节点最后一次尝试的原始异常）；失败节点记录 `failed` 及最后异常的 `type` 与 `message`，已完成节点保留结果，未启动及依赖失败的节点保持 `pending`。`continue_on_error=True` 时，独立可运行节点继续推进，直接或间接依赖失败节点的任务不被调用，全部可调度工作结束后仍报告稳定拓扑序中最早的失败；无失败时返回完整（或 `targets` 闭包内）的结果。

