from collections import defaultdict,deque
class TaskGraph:
    def __init__(self): self.tasks={}; self.deps=defaultdict(set)
    def add(self,name,fn,depends=()):
        if name in self.tasks: raise ValueError("duplicate task")
        self.tasks[name]=fn; self.deps[name]=set(depends)
    def order(self):
        deps={k:set(v) for k,v in self.deps.items()}; out=[]; q=deque(sorted(k for k,v in deps.items() if not v))
        while q:
            n=q.popleft(); out.append(n)
            for child in sorted(deps):
                if n in deps[child]:
                    deps[child].remove(n)
                    if not deps[child]: q.append(child)
        if len(out)!=len(deps): raise ValueError("cycle detected")
        return out
    def run(self):
        results={}
        for name in self.order(): results[name]=self.tasks[name](results)
        return results
