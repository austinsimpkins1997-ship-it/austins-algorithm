"""Explicit finite planning through Python; no engine or provider discovery."""
from austins_algorithm.planning_api import run_request

problem={'name':'Two ways to cover one requirement','variables':[{'name':'Existing equipment','cost':2},{'name':'New equipment','cost':8}],'clauses':[[1,2]]}
result=run_request({'schema_version':'chronorealm.planning/1','problem':problem,'budget':{'nodes':1000,'seconds':5},'backend':'branch-bound'})
print(result['overview'])
