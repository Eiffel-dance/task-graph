# Task Graph

A dependency-free Python reference implementation for workflow, task-planning, scheduling.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个可校验的任务有向无环图执行器。它应支持添加带依赖的任务、检测重复节点与环、按稳定拓扑序执行，并在失败时保留已完成和失败节点的可观察状态。调度结果必须可复现，任务函数只接收明确输入，后续可在同一接口上接入并发和重试策略。

## Priority scheduling

`add(name, fn, depends=..., *, priority=n)` 增加一个仅限关键字传入的整数优先级（省略时按 `0` 处理，位置参数解释不变）。优先级允许负数；数值越大越优先，布尔值即使是 `int` 子类也被拒绝（连同浮点数、字符串等非整数一律抛出 `TypeError`）。非法值在任何结构变更前抛出，注册失败不会在图中留下任务、依赖或优先级记录，也不改变最近一次 `execution_state()` 快照；既有的重复名称、空名称、不可调用函数与依赖参数校验结果完全不变。

当多个节点的直接依赖都已满足时，调度器先选择 `priority` 较大者；同优先级按既有入队顺序打破平局（首批无依赖节点及同一批被解锁的兄弟节点内部按任务名称升序），因此默认全部为 `0` 时，`order()`、`plan()` 与 `run()` 的序列与旧的稳定名称序列逐字一致，与注册先后无关。优先级只在"已就绪"的节点之间排序，依赖节点始终先于其后继：一个高优先级节点仍会等待它的全部直接依赖完成。

`order()` 与 `plan()` 返回同一套优先级拓扑序；`targets` 仍先对完整图完成缺失依赖与环校验，再把该优先级拓扑序投影到目标闭包。顺序模式下 `run()` 严格按该序列调用任务；`max_concurrency=k>1` 时，每批从就绪节点中按该序列取前 `k` 个并等待整批收束。成功结果映射与 `execution_state()` 快照的键序都遵循该序列，不受线程完成先后影响。任务函数仍只接收直接依赖结果组成的全新输入映射，输入键序规则（直接依赖名称升序）不变。

重试预算、`retry_limits`、`continue_on_error`、失败与不可达节点状态、`TaskExecutionError`、`cancel_check` 的轮询时机以及 `TaskCancelledError`/`TaskControlError` 语义全部保留，只把其中引用的稳定拓扑顺序替换为新的优先级拓扑顺序（例如最终报告的失败节点与取消名单中的节点次序）。目标闭包外的节点仍不执行也不进入快照。优先级只影响调度选择：不改变任务函数收到的直接依赖结果与输入键序、图校验范围、快照字段，也不改变后续不传 `priority`/不传新参数运行时的既有兼容性。

## Retry policy

`run(max_retries=n)` 为可选的按任务重试策略：`n` 表示每个任务在首次失败后允许再次调用的次数（默认 `0`，即每个任务只调用一次）。`n` 必须是非负整数（布尔值无效），非法值在任何任务执行前抛出 `ValueError`，且不影响上一轮 `execution_state()` 快照。任务抛异常时立即重试同一任务，重试预算按任务独立计算；每次尝试都收到只含直接依赖结果的全新映射。耗尽次数仍失败时抛出 `TaskExecutionError`（`task_name`、`original` 与 `__cause__` 均指向最后一次异常），下游任务不执行。

## Per-task retry limits

`run(retry_limits={...})` 增加一个仅限关键字传入的映射，将任务名映射到非负整数，为单个任务覆盖 `max_retries`：映射中的数值表示该任务首次失败后允许再次调用的次数，没有映射项的任务继续使用 `max_retries`。省略或传入 `None` 时完全保持统一的 `max_retries` 行为，位置参数解释不变。每个任务的预算独立计算，不因其他任务消耗预算而改变；每次尝试仍收到只含直接依赖结果的全新输入映射，返回映射键序仍遵循优先级拓扑序。

`retry_limits` 的整体校验在排序、调用任务和替换最近一次 `execution_state()` 快照之前完成：输入不是映射、键不是字符串、值不是非布尔整数时抛出 `TypeError`；负数值抛出 `ValueError`；引用未注册任务的键抛出 `KeyError`。这些输入错误均不执行任何任务，也不改变图或上一份快照。校验与 `targets` 协同：映射中未被目标闭包选中的合法任务可以保留但不生效，闭包内任务按各自配置独立计算重试次数。

任务耗尽自己的预算仍抛异常时沿用既有失败语义（`continue_on_error` 的快速失败/继续执行、`TaskExecutionError` 指向最后一次异常、失败记录最后一次异常的 `type` 与 `message`、重试成功只显示最终结果），并与 `max_concurrency` 并发调度完全兼容。

## Continue-on-error scheduling

`run(continue_on_error=True)` 开启可选的非快速失败调度（默认 `False`，既有快速失败语义完全不变）。运行仍严格按优先级拓扑序推进，每次任务调用仍只接收由直接依赖结果组成的全新映射，重试预算仍按任务独立计算。

开启后，任务耗尽 `max_retries` 仍抛异常时立即记录为 `failed`，随后继续执行尚未受影响的独立任务；任何直接或间接依赖失败任务的节点都不会被调用，保持 `pending`，其 `result` 与 `error` 均为空值（`None`）。所有可运行节点处理完后，只要存在失败仍抛出 `TaskExecutionError`，并以优先级拓扑序中最早的失败节点作为异常主体（`task_name`、`original`、`__cause__` 指向该节点最后一次尝试的原始异常）。`execution_state()` 的独立快照同时反映已完成、失败以及因依赖失败而未执行的节点；若无失败，返回映射的内容与键顺序与默认行为一致。

`continue_on_error` 只接受布尔值：传入其他类型在任何任务执行或状态替换前抛出 `TypeError`，并保留上一份 `execution_state()` 快照。

## Target selection

`run(targets=[...])` 增加一个仅限关键字传入的可迭代任务名集合（省略或传 `None` 时完全保持全图行为，位置参数解释不变）。执行范围为每个目标及其全部传递依赖，重复目标只执行一次；实际执行顺序是完整图优先级拓扑序在该闭包上的投影，范围之外的任务不会被调用，闭包内任务收到的直接依赖输入映射与全图运行完全一致，返回映射包含闭包内所有成功完成的任务且键按该投影顺序排列。

`targets` 的整体校验在排序、执行和替换最近一次状态快照之前完成：字符串、bytes 或其他不可迭代对象、集合中的非字符串名称抛出 `TypeError`；空名称或空集合抛出 `ValueError`；引用不存在的任务抛出 `KeyError`。这些输入错误均不执行任何任务并保留上一份 `execution_state()` 快照。目标模式下仍先对**整张图**完成既有的缺失依赖（`KeyError`）与环（`ValueError`）校验，未选中的坏节点不会被静默当作有效图。

失败处理沿用既有规则：`max_retries` 按任务独立计算，`continue_on_error` 下只推进不依赖失败节点的已选任务，失败节点的下游保持 `pending`，最终仍报告优先级拓扑序中最早的 `TaskExecutionError`。一次目标运行结束后 `execution_state()` 只记录本次闭包内的节点（记录字段与快照独立性不变），闭包外节点既不执行也不出现在快照中；`targets` 省略或为 `None` 时仍记录整张图。

## Concurrency

`run(max_concurrency=k)` 增加一个仅限关键字传入的并发上限（默认 `1`，此时任务调用次序、返回结果与 `execution_state()` 与既有顺序执行完全一致）。`k` 必须是正整数：布尔值或其他非整数类型抛出 `TypeError`，不大于零的整数抛出 `ValueError`；校验在任何任务执行、排序和状态快照替换之前完成，失败时保留上一份 `execution_state()` 快照。

`k > 1` 时启用分批并发调度：仍先对整张图完成既有的缺失依赖（`KeyError`）与环（`ValueError`）校验，然后按优先级拓扑序确定就绪节点（全部直接依赖已成功完成），每轮最多启动 `k` 个任务并等待本批次全部收束后才进入下一轮。任务函数仍只收到由直接依赖结果组成的全新输入映射，重试预算仍按任务独立计算；输入映射的键序、返回映射的键序以及 `execution_state()` 的记录顺序始终按优先级拓扑序确定，任务完成的先后不改变任何可观察顺序。

失败语义与顺序执行一致：`continue_on_error=False`（默认）时，若批次中有任务耗尽重试仍失败，调度器等待本批次已启动的任务收束后停止启动新节点，抛出优先级拓扑序中最早失败节点的 `TaskExecutionError`（`task_name`、`original`、`__cause__` 指向该节点最后一次尝试的原始异常）；失败节点记录 `failed` 及最后异常的 `type` 与 `message`，已完成节点保留结果，未启动及依赖失败的节点保持 `pending`。`continue_on_error=True` 时，独立可运行节点继续推进，直接或间接依赖失败节点的任务不被调用，全部可调度工作结束后仍报告优先级拓扑序中最早的失败；无失败时返回完整（或 `targets` 闭包内）的结果。

## Execution plan preview

`plan(targets=None)` 是只读的执行计划查询入口：在调用任何任务函数之前，针对整张图或指定 `targets` 计算本次运行覆盖的任务闭包，返回按优先级拓扑规则排列的任务名称列表。省略 `targets` 或传入 `None` 时覆盖完整图；传入可迭代任务名集合时先去重，再包含每个目标的全部传递依赖，结果是完整图优先级拓扑序列在闭包上的投影（目标没有依赖时只返回该目标）。每次成功调用都返回全新的列表，调用方修改返回值不影响图本身或后续查询。

`plan` 对 `targets` 的校验与 `run` 完全一致，且在排序之前完成：字符串、bytes 或其他不可迭代对象以及集合中的非字符串名称抛出 `TypeError`；空名称或空集合抛出 `ValueError`；引用不存在的任务抛出 `KeyError`。输入通过后仍对**整张图**完成缺失依赖（`KeyError`）与环（`ValueError`）校验，问题节点不在目标闭包内也不被忽略。任何校验失败都不会调用任务函数，也不会创建或替换最近一次 `execution_state()` 快照；成功的 `plan` 调用同样不改变任务注册、依赖关系或执行状态。

`plan` 的成功结果就是 `run` 的调度契约：对同一份未变更的图和同一组 `targets`，`plan` 返回的列表与实际 `run` 采用的优先级任务序列、返回映射键顺序以及 `execution_state()` 的记录顺序完全一致。该序列只由任务名称、优先级和依赖关系决定，不受注册先后、集合遍历、任务完成时序、`max_retries`、`continue_on_error` 或 `max_concurrency` 影响。`plan` 只负责预览，不执行任务，也不改变既有的失败与并发语义。

## Cooperative cancellation

`run(cancel_check=fn)` 增加一个仅限关键字传入的无参可调用对象（省略或传 `None` 时调用顺序、返回结果、异常类型、快照字段与失败传播规则完全不变，位置参数解释也不变）。传入非 `None` 的不可调用对象在排序、执行和替换最近一次 `execution_state()` 快照之前抛出 `TypeError`；回调是否可零参数调用不在此检查范围内——调用时的参数错误按回调自身异常处理（见下）。

调度器在顺序模式下、每个**即将启动**的任务前（已因依赖失败而不可达的节点除外），按优先级拓扑序调用回调一次；并发模式下在每轮批次准备就绪后、提交任何任务前轮询一次。回调返回任意真值即记录取消请求：

- 首次观察到取消后，不再启动任何尚未开始的任务，也不为它们消耗重试次数（取消前任务正在执行的重试预算继续自然收束）；并发模式下已提交的批次全部等待结束后才处理结果，结果仍按优先级拓扑序写入。
- 本次运行固定抛出 `TaskCancelledError`，其 `task_names` 按优先级拓扑序列出闭包内所有未启动节点。
- 快照中：已成功节点保留 `completed` 与结果；已失败节点保留 `failed` 及最后一次错误；其余未启动节点（含因依赖失败而不可达的节点）一律为 `cancelled`，`result` 与 `error` 均为 `None`。快照仍只覆盖本次 `targets` 闭包，闭包外节点不记录，快照独立性不变。
- 仅当取消尚未被观察且任务按既有规则失败时，才抛 `TaskExecutionError`（快速失败或 `continue_on_error` 末尾报告最早失败的规则不变）；一旦回调已返回真值，即使快照中留有已记录失败，本次运行也固定抛 `TaskCancelledError`。

回调自身抛出异常时，包装为 `TaskControlError`（`original` 与 `__cause__` 均指向回调异常），已完成/失败的状态保留，未启动节点标记为 `cancelled`；该异常不会替换图校验（`KeyError`/`ValueError`）、参数校验（`TypeError`/`ValueError`/`KeyError`）或任务自身的 `TaskExecutionError`。回调不接收参数，其返回值可为任意真值（用真值判断）。

取消不改变图结构：重试预算、目标闭包、直接依赖输入、优先级拓扑键序与成功返回键序的既有语义均不受影响；后续不传入 `cancel_check` 的 `run` 仍全新执行。`plan` 不接受也不会调用该回调。

## Resumable execution

`run(resume=True)` 增加一个仅限关键字传入的布尔开关（省略或传 `False` 时，`run` 的位置参数、校验、调用与快照语义与历史行为完全一致：每次都新建快照并重调全部任务）。非布尔值（`0`、`1`、字符串、`None` 等）在排序、任务调用与快照替换之前抛出 `TypeError` 并保留上一份 `execution_state()` 快照。

`resume=True` 时先完成既有的参数校验与整图校验（缺失依赖 `KeyError`、环 `ValueError` 等照常先于恢复判定），再确认运行快照：

- 此前没有任何运行快照（仅 `plan`/`order`/`add` 不算运行；空图成功运行也会留下快照）时抛 `TaskResumeError`，`reason="no_previous_run"`。
- 快照生成时的任务名称集合、任一任务的直接依赖或 `priority` 与当前图不同时抛同一异常，`reason="graph_changed"`。
- 图结构未变但本次 `targets` 规范化后的闭包成员集合与快照范围不同时抛同一异常，`reason="scope_changed"`。

以上拒绝都发生在任何任务调用与快照替换之前，旧快照原样保留。

通过确认后，仅复用快照中 `status` 为 `completed` 的节点：这些任务不会被再次调用，不占用 `max_concurrency` 名额，也不参与取消轮询；其旧结果直接进入下游输入映射与返回映射。`failed`、`pending`、`cancelled` 节点清除旧的 `result`/`error`，按同一套优先级拓扑序重新调度：只有全部直接依赖（含复用节点）成功后才启动；旧失败曾阻塞的下游节点，在依赖本次恢复成功后也会参加执行。被重调的任务继续收到只含直接依赖结果的全新映射（键仍按直接依赖名称升序），`max_retries` 与 `retry_limits` 按本次恢复重新计数，上一轮已消耗的尝试不计入；结果映射与快照记录都按优先级拓扑序排列，并同时保留复用结果与新结果。

恢复运行完全兼容 `max_concurrency`、`continue_on_error` 与 `cancel_check`：分批时每批只从未完成节点中挑选，复用的完成节点不占名额；快速失败、继续执行、`TaskExecutionError`（以优先级拓扑序最早失败节点为主体）、失败记录以及依赖失败节点保持 `pending` 的规则全部沿用；取消时复用节点保持 `completed`，`TaskCancelledError.task_names` 只按优先级拓扑序列出本次尚未启动的节点，其余状态沿用既有取消规则；回调自身抛异常仍包装为 `TaskControlError`。若成功恢复后闭包内节点已全部为 `completed`，则不调用任何任务，直接按原序返回全部结果并保留旧快照。`plan`、`order`、`add` 以及不传 `resume` 的 `run` 均不受影响；`execution_state()` 仍返回相互独立的快照副本。

## Execution trace

`execution_trace()` 是只读执行审计入口：返回最近一次**建立执行快照**的那次 `run` 留下的轨迹，是一个全新的列表，按该次运行的优先级拓扑顺序为闭包内每个任务保存一条记录。尚无运行快照时返回空列表；`plan`、`order`、`add` 以及参数校验或整图校验失败的调用都不生成也不替换轨迹（全完成节点的 `resume=True` 不建立新快照，轨迹同样保持原样）。每次调用都返回深拷贝的新列表，调用方修改返回值或其中的记录不影响图内部状态与后续查询。

每条记录包含五个字段：`task_name`（任务名）、`status`、`attempts`、`errors`、`error`。`attempts` 是本次运行实际调用该任务的次数（重试计入，未调用的任务为 `0`）；`errors` 按调用次序保存每次失败的 `{"type", "message"}` 摘要；`error` 是最终异常的同样摘要或 `None`。`status` 取值：`completed`（调用并成功）、`failed`（耗尽重试后最终失败）、`blocked`（`continue_on_error` 下因失败依赖而未被调用）、`cancelled`（取消或控制回调异常导致未启动）、`reused`（`resume=True` 复用上一快照的 `completed` 结果）、`pending`（快速失败前来不及调度）。重试只增加同一记录的 `attempts` 并向 `errors` 追加，最终错误仍取最后一次异常；重试后成功的记录保留此前各次失败摘要，但 `error` 为 `None`。

并发调度下轨迹的记录顺序与 `attempts` 不受线程完成先后影响：各批次的结果在收束后按优先级拓扑顺序归并到同一套记录中。目标闭包外的任务不出现在轨迹中。`cancel_check`、`TaskControlError`、`TaskCancelledError`、`TaskResumeError` 及既有 `KeyError`/`ValueError`/`TypeError` 的触发条件与优先级保持不变；控制回调异常时已开始任务的记录原样保留，其余任务标记 `cancelled`。`execution_state()` 的字段、快照独立性、返回映射顺序与失败传播规则均不改变。

