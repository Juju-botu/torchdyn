"""
	Functional API of ODE integration routines, with specialized functions for different options
	`odeint` and `odeint_mshooting` prepare and redirect to more specialized routines, detected automatically.
"""
from inspect import getargspec
from typing import List, Tuple, Union, Callable
from warnings import warn

import torch
from torch import Tensor
import torch.nn as nn

from torchdyn.numerics.solvers import AsynchronousLeapfrog, str_to_solver, str_to_ms_solver
from torchdyn.numerics.interpolators import str_to_interp
from torchdyn.numerics.utils import hairer_norm, init_step, adapt_step, EventState


def odeint(f:Callable, x:Tensor, t_span:Union[List, Tensor], solver:Union[str, nn.Module], atol:float=1e-3, rtol:float=1e-3, 
		   t_stops:Union[List, Tensor, None]=None, verbose:bool=False, interpolator:Union[str, Callable, None]=None, return_all_eval:bool=False, 
		   seminorm:Tuple[bool, Union[int, None]]=(False, None)) -> Tuple[Tensor, Tensor]:
	"""[summary]

	Args:
		f (Callable): [description]
		x (Tensor): [description]
		t_span (Union[List, Tensor]): [description]
		solver (Union[str, nn.Module]): [description]
		atol (float, optional): [description]. Defaults to 1e-3.
		rtol (float, optional): [description]. Defaults to 1e-3.
		t_stops (Union[List, Tensor, None], optional): [description]. Defaults to None.
		verbose (bool, optional): [description]. Defaults to False.
		use_interp (bool, optional): [description]. Defaults to False.
		return_all_eval (bool, optional): [description]. Defaults to False.
		seminorm (Tuple[bool, Union[int, None]], optional): [description]. Defaults to (False, None).

	Raises:
		NotImplementedError: [description]

	Returns:
		Tuple[Tensor, Tensor]: [description]
	"""
	if t_span[1] < t_span[0]: # time is reversed
		if verbose: warn("You are integrating on a reversed time domain, adjusting the vector field automatically")
		f_ = lambda t, x: -f(-t, x)
		t_span = -t_span
	else: f_ = f

	if type(t_span) == list: t_span = torch.cat(t_span)
	# instantiate the solver in case the user has specified preference via a `str` and ensure compatibility of device ~ dtype
	if type(solver) == str:
		solver = str_to_solver(solver, x.dtype)
	x, t_span = solver.sync_device_dtype(x, t_span)
	stepping_class = solver.stepping_class

	# instantiate the interpolator similar to the solver steps above
	if type(interpolator) == str: 
		interpolator = str_to_interp(interpolator, x.dtype)
		x, t_span = interpolator.sync_device_dtype(x, t_span)

	# access parallel integration routines with different t_spans for each sample in `x`.
	if len(t_span.shape) > 1:
		raise NotImplementedError("Parallel routines not implemented yet, check experimental versions of `torchdyn`")
	# odeint routine with a single t_span for all samples
	elif len(t_span.shape) == 1:
		if stepping_class == 'fixed':
			if atol != odeint.__defaults__[0] or rtol != odeint.__defaults__[1]:
				warn("Setting tolerances has no effect on fixed-step methods")
			return _fixed_odeint(f_, x, t_span, solver) 
		elif stepping_class == 'adaptive':
			t = t_span[0]
			k1 = f_(t, x)
			dt = init_step(f, k1, x, t, solver.order, atol, rtol)
			return _adaptive_odeint(f_, k1, x, dt, t_span, solver, atol, rtol, interpolator, return_all_eval, seminorm)


# TODO (qol) state augmentation for symplectic methods 
def odeint_symplectic(f:Callable, x:Tensor, t_span:Union[List, Tensor], solver:Union[str, nn.Module], atol:float=1e-3, rtol:float=1e-3, 
		   verbose:bool=False, return_all_eval:bool=False):
	if t_span[1] < t_span[0]: # time is reversed
		if verbose: warn("You are integrating on a reversed time domain, adjusting the vector field automatically")
		f_ = lambda t, x: -f(-t, x)
		t_span = -t_span
	else: f_ = f
	if type(t_span) == list: t_span = torch.cat(t_span)

	# instantiate the solver in case the user has specified preference via a `str` and ensure compatibility of device ~ dtype
	if type(solver) == str:
		solver = str_to_solver(solver, x.dtype)
	x, t_span = solver.sync_device_dtype(x, t_span)
	stepping_class = solver.stepping_class

	# additional bookkeeping for symplectic solvers
	if not hasattr(f, 'order'):
		raise RuntimeError('The system order should be specified as an attribute `order` of `vector_field`')
	if isinstance(solver, AsynchronousLeapfrog) and f.order == 2: 
		raise RuntimeError('ALF solver should be given a vector field specified as a first-order symplectic system: v = f(t, x)')
	solver.x_shape = x.shape[-1] // 2

	# access parallel integration routines with different t_spans for each sample in `x`.
	if len(t_span.shape) > 1:
		raise NotImplementedError("Parallel routines not implemented yet, check experimental versions of `torchdyn`")
	# odeint routine with a single t_span for all samples
	elif len(t_span.shape) == 1:
		if stepping_class == 'fixed':
			if atol != odeint_symplectic.__defaults__[0] or rtol != odeint_symplectic.__defaults__[1]:
				warn("Setting tolerances has no effect on fixed-step methods")
			return _fixed_odeint(f_, x, t_span, solver) 
		elif stepping_class == 'adaptive':
			t = t_span[0]
			if f.order == 1: 
				pos = x[..., : solver.x_shape]
				k1 = f(t, pos)
				dt = init_step(f, k1, pos, t, solver.order, atol, rtol)
			else:
				k1 = f(t, x)
				dt = init_step(f, k1, x, t, solver.order, atol, rtol)
			return _adaptive_odeint(f_, k1, x, dt, t_span, solver, atol, rtol, return_all_eval)


def odeint_mshooting(f:Callable, x:Tensor, t_span:Tensor, solver:Union[str, nn.Module], B0=None, fine_steps=2, maxiter=4):
	if type(solver) == str:
		solver = str_to_ms_solver(solver)
	x, t_span = solver.sync_device_dtype(x, t_span)
	# first-guess B0 of shooting parameters
	if B0 is None:
		_, B0 = odeint(f, x, t_span, solver.coarse_method)
	# determine which odeint to apply to MS solver
	# TODO (qol): automatically detect if time-variant ODE and use `_shifted_odeint`
	odeint_func = _fixed_odeint
	###
	B = solver.root_solve(odeint_func, f, x, t_span, B0, fine_steps, maxiter)
	return t_span, B



def odeint_hybrid(f, x, t_span, j_span, solver, callbacks, atol=1e-3, rtol=1e-3, event_tol=1e-4, priority='jump',
				  seminorm:Tuple[bool, Union[int, None]]=(False, None)):
	"""[summary]

	Args:
		f ([type]): [description]
		x ([type]): [description]
		t_span ([type]): [description]
		j_span ([type]): [description]
		solver ([type]): [description]
		callbacks ([type]): [description]
		t_eval (list, optional): [description]. Defaults to [].
		atol ([type], optional): [description]. Defaults to 1e-3.
		rtol ([type], optional): [description]. Defaults to 1e-3.
		event_tol ([type], optional): [description]. Defaults to 1e-4.
		priority (str, optional): [description]. Defaults to 'jump'.

	Returns:
		[type]: [description]
	"""
	# instantiate the solver in case the user has specified preference via a `str` and ensure compatibility of device ~ dtype
	if type(solver) == str: solver = str_to_solver(solver, x.dtype)
	x, t_span = solver.sync_device_dtype(x, t_span)
	x_shape = x.shape
	ckpt_counter, ckpt_flag, jnum = 0, False, 0
	t_eval, t, T = t_span[1:], t_span[:1], t_span[-1]
	
	###### initial jumps ###########
	event_states = EventState([False for _ in range(len(callbacks))])

	if priority == 'jump':
		new_event_states = EventState([cb.check_event(t, x) for cb in callbacks])
		triggered_events = event_states != new_event_states
		# check if any event flag changed from `False` to `True` within last step
		triggered_events = sum([(a_ != b_)  & (b_ == False)
								for a_, b_ in zip(new_event_states.evid, event_states.evid)])
		if triggered_events > 0:
			i = min([i for i, idx in enumerate(new_event_states.evid) if idx == True])
			x = callbacks[i].jump_map(t, x)
			jnum = jnum + 1

	################## initial step size setting ################
	k1 = f(t, x)
	dt = init_step(f, k1, x, t, solver.order, atol, rtol)

	#### init solution & time vector ####
	eval_times, sol = [t], [x]

	while t < T and jnum < j_span:
		
		############### checkpointing ###############################
		if t + dt > t_span[-1]:
			dt = t_span[-1] - t
		if t_eval is not None:
			#print(ckpt_counter, len(t_eval), t+dt, t_eval[ckpt_counter])
			if (ckpt_counter < len(t_eval)) and (t + dt > t_eval[ckpt_counter]):
				#print("GOING IN")
				dt_old, ckpt_flag = dt, True
				dt = t_eval[ckpt_counter] - t
				ckpt_counter += 1
		#print('t, dt', t, dt)

		################ step
		f_new, x_new, x_err, _ = solver.step(f, x, t, dt, k1=k1)

		################ callback and events ########################
		new_event_states = EventState([cb.check_event(t + dt, x_new) for cb in callbacks])
		triggered_events = sum([(a_ != b_)  & (b_ == False)
								for a_, b_ in zip(new_event_states.evid, event_states.evid)])


		# if event, close in on switching state in [t, t + Δt] via bisection
		if triggered_events > 0:
			
			dt_pre, t_inner, dt_inner, x_inner, niters = dt, t, dt, x, 0
			max_iters = 100  # TODO (numerics): compute tol as function of tolerances

			while niters < max_iters and event_tol < dt_inner:
				with torch.no_grad():
					dt_inner = dt_inner / 2
					f_new, x_, x_err, _ = solver.step(f, x_inner, t_inner, dt_inner, k1=k1)

					new_event_states = EventState([cb.check_event(t_inner + dt_inner, x_)
												   for cb in callbacks])
					triggered_events = sum([(a_ != b_)  & (b_ == False)
											for a_, b_ in zip(new_event_states.evid, event_states.evid)])
					niters = niters + 1

				if triggered_events == 0: # if no event, advance start point of bisection search
					x_inner = x_
					t_inner = t_inner + dt_inner
					dt_inner = dt
					k1 = f_new
					# TODO (qol): optional save
					#sol.append(x_inner.reshape(x_shape))
					#eval_times.append(t_inner.reshape(t.shape))
			x = x_inner
			t = t_inner
			i = min([i for i, x in enumerate(new_event_states.evid) if x == True])

			# save state and time BEFORE jump
			sol.append(x.reshape(x_shape))
			eval_times.append(t.reshape(t.shape))

			# apply jump func.
			x = callbacks[i].jump_map(t, x)

			# save state and time AFTER jump
			sol.append(x.reshape(x_shape))
			eval_times.append(t.reshape(t.shape))

			# reset k1
			k1 = None
			dt = dt_pre

		else:
			################# compute error #############################
			if seminorm[0] == True: 
				state_dim = seminorm[1]
				error = x_err[:state_dim]
				error_scaled = error / (atol + rtol * torch.max(x[:state_dim].abs(), x_new[:state_dim].abs()))
			else: 
				error = x_err
				error_scaled = error / (atol + rtol * torch.max(x.abs(), x_new.abs()))
			
			error_ratio = hairer_norm(error_scaled)
			accept_step = error_ratio <= 1

			if accept_step:
				t = t + dt
				x = x_new
				sol.append(x.reshape(x_shape))
				eval_times.append(t.reshape(t.shape))
				k1 = f_new

			if ckpt_flag:
				dt = dt_old - dt
				ckpt_flag = False
			################ stepsize control ###########################
			dt = adapt_step(dt, error_ratio,
							solver.safety,
							solver.min_factor,
							solver.max_factor,
							solver.order)

	return torch.cat(eval_times), torch.stack(sol)


def _adaptive_odeint(f, k1, x, dt, t_span, solver, atol=1e-4, rtol=1e-4, interpolator=None, return_all_eval=False, seminorm=(False, None)):
	"""
	
	Notes:
	(1) We check if the user wants all evaluated solution points, not only those
	corresponding to times in `t_span`. This is automatically set to `True` when `odeint`
	is called for interpolated adjoints


	Args:
		f ([type]): [description]
		k1 ([type]): [description]
		x ([type]): [description]
		dt ([type]): [description]
		t_span ([type]): [description]
		solver ([type]): [description]
		atol ([type], optional): [description]. Defaults to 1e-4.
		rtol ([type], optional): [description]. Defaults to 1e-4.
		use_interp (bool, optional):
		return_all_eval (bool, optional): [description]. Defaults to False.

	Returns:
		[type]: [description]
	
	"""
	t_eval, t, T = t_span[1:], t_span[:1], t_span[-1]
	ckpt_counter, ckpt_flag = 0, False	
	eval_times, sol = [t], [x]
	while t < T:
		if t + dt > T: 
			dt = T - t
		############### checkpointing ###############################
		if t_eval is not None:
			# satisfy checkpointing by using interpolation scheme or resetting `dt`
			if (ckpt_counter < len(t_eval)) and (t + dt > t_eval[ckpt_counter]):
				if interpolator == None:	
					# save old dt, raise "checkpoint" flag and repeat step
					dt_old, ckpt_flag = dt, True
					dt = t_eval[ckpt_counter] - t

		f_new, x_new, x_err, stages = solver.step(f, x, t, dt, k1=k1)
		################# compute error #############################
		if seminorm[0] == True: 
			state_dim = seminorm[1]
			error = x_err[:state_dim]
			error_scaled = error / (atol + rtol * torch.max(x[:state_dim].abs(), x_new[:state_dim].abs()))
		else: 
			error = x_err
			error_scaled = error / (atol + rtol * torch.max(x.abs(), x_new.abs()))
		error_ratio = hairer_norm(error_scaled)
		accept_step = error_ratio <= 1

		if accept_step:
			############### checkpointing via interpolation ###############################
			if t_eval is not None and interpolator is not None:
				coefs = None
				while (ckpt_counter < len(t_eval)) and (t + dt > t_eval[ckpt_counter]):
					t0, t1 = t, t + dt
					x_mid = x + dt * sum([interpolator.bmid[i] * stages[i] for i in range(len(stages))])
					f0, f1, x0, x1 = k1, f_new, x, x_new
					if coefs == None: coefs = interpolator.fit(dt, f0, f1, x0, x1, x_mid)
					x_in = interpolator.evaluate(coefs, t0, t1, t_eval[ckpt_counter])
					sol.append(x_in)
					eval_times.append(t_eval[ckpt_counter][None])
					ckpt_counter += 1

			if t + dt == t_eval[ckpt_counter] or return_all_eval: # note (1)
				sol.append(x_new)
				eval_times.append(t + dt)
				# we only increment the ckpt counter if the solution points corresponds to a time point in `t_span`
				if t + dt == t_eval[ckpt_counter]: ckpt_counter += 1
			t, x = t + dt, x_new
			k1 = f_new 

		################ stepsize control ###########################
		# reset "dt" in case of checkpoint without interp
		if ckpt_flag:
			dt = dt_old - dt
			ckpt_flag = False
			
		dt = adapt_step(dt, error_ratio,
						solver.safety,
						solver.min_factor,
						solver.max_factor,
						solver.order)
	return torch.cat(eval_times), torch.stack(sol)


def _fixed_odeint(f, x, t_span, solver):
	"""Solves IVPs with same `t_span`, using fixed-step methods

	Args:
		f ([type]): [description]
		x ([type]): [description]
		t_span ([type]): [description]
		solver ([type]): [description]

	Returns:
		[type]: [description]
	"""
	t, T, dt = t_span[0], t_span[-1], t_span[1] - t_span[0]
	sol = [x]
	steps = 1
	while steps <= len(t_span) - 1:
		_, x, _ = solver.step(f, x, t, dt)
		sol.append(x)
		t = t + dt
		if steps < len(t_span) - 1: dt = t_span[steps+1] - t
		steps += 1
	return t_span, torch.stack(sol)


# TODO: update dt
def _shifted_fixed_odeint(f, x, t_span):
	"""Solves ``n_segments'' jagged IVPs in parallel with fixed-step methods. All subproblems
	have equal step sizes and number of solution points"""
	t, T = t_span[..., 0], t_span[..., -1]
	dt = t_span[..., 1] - t
	sol, k1 = [], f(t, x)

	not_converged = ~((t - T).abs() <= 1e-6).bool()
	while not_converged.any():
		x[:, ~not_converged] = torch.zeros_like(x[:, ~not_converged])
		k1, _, x = solver.step(f, x, t, dt[..., None], k1=k1)  # dt will be broadcasted on dim1
		sol.append(x)
		t = t + dt
		not_converged = ~((t - T).abs() <= 1e-6).bool()
	# stacking is only possible since the number of steps in each of the ``n_segments''
	# is assumed to be the same. Otherwise require jagged tensors or a []
	return torch.stack(sol)



def _jagged_fixed_odeint(f, x,
						t_span: List, solver):
	"""
	Solves ``n_segments'' jagged IVPs in parallel with fixed-step methods. Each sub-IVP can vary in number
    of solution steps and step sizes

	Args:
		f:
		x:
		t_span:
		solver:

	Returns:
		A list of `len(t_span)' containing solutions of each IVP computed in parallel.
	"""
	t, T = [t_sub[0] for t_sub in t_span], [t_sub[-1] for t_sub in t_span]
	t, T = torch.stack(t), torch.stack(T)

	dt = torch.stack([t_[1] - t0 for t_, t0 in zip(t_span, t)])
	sol = [[x_] for x_ in x]
	not_converged = ~((t - T).abs() <= 1e-6).bool()
	steps = 0
	while not_converged.any():
		_, _, x = solver.step(f, x, t, dt[..., None, None])  # dt will be to x dims

		for n, sol_ in enumerate(sol):
			sol_.append(x[n])
		t = t + dt
		not_converged = ~((t - T).abs() <= 1e-6).bool()

		steps += 1
		dt = []
		for t_, tcur in zip(t_span, t):
			if steps > len(t_) - 1:
				dt.append(torch.zeros_like(tcur))  # subproblem already solved
			else:
				dt.append(t_[steps] - tcur)

		dt = torch.stack(dt)
	# prune solutions to remove noop steps
	sol = [sol_[:len(t_)] for sol_, t_ in zip(sol, t_span)]
	return [torch.stack(sol_, 0) for sol_ in sol]
	