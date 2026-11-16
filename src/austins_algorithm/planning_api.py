"""Portable planning v1 request contract. Existing quantum adapters stay separate."""
from .planning import API_VERSION, MAX_VARIABLES, MAX_CLAUSES, solve, overview, problem_from_document


def capabilities():
    return {'schema_version':API_VERSION,'execution_mode':'CLASSICAL','max_variables':MAX_VARIABLES,
            'max_clauses':MAX_CLAUSES,'backends':['branch-bound','scipy-milp'],
            'resume_backend':'branch-bound','authority':'NONE','schema':problem_schema()}


def run_request(request):
    if not isinstance(request,dict) or set(request)!={'schema_version','problem','budget','backend'} or request['schema_version']!=API_VERSION:
        raise ValueError('Unsupported planning request schema/version')
    problem=problem_from_document(request['problem'])
    result=solve(problem,budget=request['budget'],backend=request['backend'])
    result['overview']=overview(problem,result,state='COMPLETED' if result['optimality']=='EXACT_FINITE_MODEL' else 'PAUSED')
    return result


def problem_schema():
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
        'title':'ChronoRealm planning problem v1','type':'object','additionalProperties':False,
        'required':['schema_version','name','variables','clauses'],
        'properties':{'schema_version':{'const':API_VERSION},'name':{'type':'string','minLength':1,'maxLength':120},
            'variables':{'type':'array','minItems':1,'maxItems':MAX_VARIABLES,'items':{'type':'object','additionalProperties':False,
                'required':['name','cost'],'properties':{'name':{'type':'string','minLength':1,'maxLength':60},'cost':{'type':'integer','minimum':0,'maximum':1000000}}}},
            'clauses':{'type':'array','maxItems':MAX_CLAUSES,'items':{'type':'array','maxItems':2*MAX_VARIABLES,
                'items':{'type':'integer','minimum':-MAX_VARIABLES,'maximum':MAX_VARIABLES,'not':{'const':0}}}}}}
