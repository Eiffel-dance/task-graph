from app import TaskGraph
g=TaskGraph(); g.add('fetch',lambda r:'data'); g.add('transform',lambda r:r['fetch'].upper(),['fetch']); g.add('publish',lambda r:len(r['transform']),['transform']); print(g.run())
