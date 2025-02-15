import papermill as pm


TORCHDYN_NOTEBOOKS_PATHS = [
    '00_quickstart.ipynb',
    'module1-neuralde/01_neural_ode_cookbook.ipynb',
    'module1-neuralde/02_crossing_trajectories.ipynb',
    'module1-neuralde/03_augmentation_strategies.ipynb',
    'module1-neuralde/04_higher_order.ipynb',
    'module2-numerics/01_hypersolver_odeint.ipynb',
    'module2-numerics/02_multiple_shooting.ipynb',
    'module2-numerics/03_hybrid_odeint.ipynb',
    'module2-numerics/04_generalized_adjoint.ipynb']


for path in TORCHDYN_NOTEBOOKS_PATHS:
    notebook_path = path.split('/')
    if len(notebook_path) == 1: 
        notebook = notebook_path[0]
        path_to_notebook = f'tutorials/{notebook}'
    else: 
        module, notebook = notebook_path
        path_to_notebook = f'tutorials/{module}/{notebook}'
    path_to_output = f'tutorials/local_nbrun_{notebook}'
    parameters=dict(dry_run=True)
    pm.execute_notebook(path_to_notebook, path_to_output, parameters=parameters)
