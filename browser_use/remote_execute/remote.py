import ast
import asyncio
import base64
import inspect
import json
import os
import sys
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, TypeVar, Union, cast, get_args, get_origin

import httpx

from browser_use.remote_execute.views import (
	BrowserCreatedData,
	ErrorData,
	LogData,
	RemoteExecutionError,
	ResultData,
	SSEEvent,
	SSEEventType,
)

T = TypeVar('T')


class NonlocalToClosureTransformer(ast.NodeTransformer):
	"""Transform nonlocal statements into closure-friendly dict patterns.

	Transforms:
		def outer():
			var1 = value1
			var2 = value2
			def inner():
				nonlocal var1, var2
				var1 = ...

	Into:
		def outer():
			_closure_vars = {'var1': value1, 'var2': value2}
			def inner():
				_closure_vars['var1'] = ...
	"""

	def __init__(self):
		self.nonlocal_vars_by_func: dict[str, set[str]] = {}
		self.current_function_stack: list[str] = []
		self.enclosing_function: str | None = None

	def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Any:
		"""Visit function definitions to track nesting and nonlocal variables."""
		# Track function nesting
		parent_func = self.current_function_stack[-1] if self.current_function_stack else None
		self.current_function_stack.append(node.name)

		# First pass: find all nonlocal statements in this function
		nonlocal_vars = set()
		for stmt in node.body:
			if isinstance(stmt, ast.Nonlocal):
				nonlocal_vars.update(stmt.names)

		if nonlocal_vars:
			# Store which variables are nonlocal for this function
			self.nonlocal_vars_by_func[node.name] = nonlocal_vars
			# Remember the enclosing function that needs transformation
			if parent_func:
				self.enclosing_function = parent_func

		# Recursively visit children
		self.generic_visit(node)

		# Pop function from stack
		self.current_function_stack.pop()

		return node

	visit_AsyncFunctionDef = visit_FunctionDef

	def transform(self, tree: ast.Module) -> ast.Module:
		"""Apply the transformation in two passes."""
		# First pass: identify nonlocal variables and their enclosing functions
		self.visit(tree)

		if not self.nonlocal_vars_by_func:
			# No nonlocal statements found, return unchanged
			return tree

		# Second pass: transform the enclosing function
		tree = self._transform_enclosing_function(tree)

		# Fix missing location info (lineno, col_offset, etc.) for all nodes
		ast.fix_missing_locations(tree)

		return tree

	def _transform_enclosing_function(self, tree: ast.Module) -> ast.Module:
		"""Transform the enclosing function to use _closure_vars dict."""
		if not self.enclosing_function or not self.nonlocal_vars_by_func:
			return tree

		# Collect all nonlocal variables across all nested functions
		all_nonlocal_vars = set()
		for vars_set in self.nonlocal_vars_by_func.values():
			all_nonlocal_vars.update(vars_set)

		# Find and transform the enclosing function
		transformer = _EnclosingFunctionTransformer(self.enclosing_function, all_nonlocal_vars, self.nonlocal_vars_by_func)
		tree = transformer.visit(tree)

		return tree


class _EnclosingFunctionTransformer(ast.NodeTransformer):
	"""Transform the enclosing function to use _closure_vars dict."""

	def __init__(self, target_func: str, nonlocal_vars: set[str], nonlocal_vars_by_func: dict[str, set[str]]):
		self.target_func = target_func
		self.nonlocal_vars = nonlocal_vars
		self.nonlocal_vars_by_func = nonlocal_vars_by_func
		self.in_target_func = False
		self.current_nested_func: str | None = None

	def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Any:
		"""Transform the target function."""
		if node.name == self.target_func and not self.in_target_func:
			# Found the enclosing function, transform it
			self.in_target_func = True

			# Find variable assignments in the function body
			var_assignments: dict[str, ast.expr] = {}
			new_body: list[ast.stmt] = []

			for stmt in node.body:
				should_skip = False
				if isinstance(stmt, ast.Assign):
					# Check if this assigns to a nonlocal variable
					for target in stmt.targets:
						if isinstance(target, ast.Name) and target.id in self.nonlocal_vars:
							# Store the assignment value
							var_assignments[target.id] = stmt.value
							# Skip this statement (don't include in new body)
							should_skip = True
							break

				if not should_skip:
					# Keep all non-matching statements
					new_body.append(stmt)

			# Create _closure_vars dict initialization
			dict_items: list[tuple[ast.expr, ast.expr]] = []
			for var_name in sorted(self.nonlocal_vars):
				value = var_assignments.get(var_name, ast.Constant(value=None))
				dict_items.append((ast.Constant(value=var_name), value))

			closure_dict: ast.stmt = ast.Assign(
				targets=[ast.Name(id='_closure_vars', ctx=ast.Store())],
				value=ast.Dict(keys=[k for k, _ in dict_items], values=[v for _, v in dict_items]),
			)

			# Insert at the beginning of function body
			node.body = [closure_dict] + new_body

			# Recursively transform nested functions
			for i, stmt in enumerate(node.body):
				node.body[i] = self.visit(stmt)

			self.in_target_func = False
			return node

		elif self.in_target_func and node.name in self.nonlocal_vars_by_func:
			# We're inside a nested function that uses nonlocal
			self.current_nested_func = node.name
			nested_nonlocal_vars = self.nonlocal_vars_by_func[node.name]

			# Remove nonlocal statements
			nested_body: list[ast.stmt] = []
			for stmt in node.body:
				if isinstance(stmt, ast.Nonlocal):
					# Skip nonlocal statements
					continue
				# Transform all other statements
				transformed_stmt = self.visit(stmt)
				if isinstance(transformed_stmt, ast.stmt):
					nested_body.append(transformed_stmt)

			node.body = nested_body

			# Transform variable references in this function
			var_transformer = _VariableReferenceTransformer(nested_nonlocal_vars)
			node = var_transformer.visit(node)

			self.current_nested_func = None
			return node

		# For other functions, just recurse
		self.generic_visit(node)
		return node

	visit_AsyncFunctionDef = visit_FunctionDef


class _VariableReferenceTransformer(ast.NodeTransformer):
	"""Transform variable references to use _closure_vars dict access."""

	def __init__(self, nonlocal_vars: set[str]):
		self.nonlocal_vars = nonlocal_vars
		self.in_nested_function = False

	def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Any:
		"""Don't transform nested functions within nested functions."""
		# Mark that we're in a nested function
		was_in_nested = self.in_nested_function
		self.in_nested_function = True

		# But don't transform doubly-nested functions
		if was_in_nested:
			return node

		self.generic_visit(node)

		self.in_nested_function = was_in_nested
		return node

	visit_AsyncFunctionDef = visit_FunctionDef

	def visit_Name(self, node: ast.Name) -> Any:
		"""Transform variable references to dict access."""
		if node.id in self.nonlocal_vars:
			# Transform to _closure_vars['var_name']
			return ast.Subscript(
				value=ast.Name(id='_closure_vars', ctx=ast.Load()), slice=ast.Constant(value=node.id), ctx=node.ctx
			)
		return node


def get_terminal_width() -> int:
	"""Get terminal width, default to 80 if unable to detect"""
	try:
		return os.get_terminal_size().columns
	except (AttributeError, OSError):
		return 80


async def _call_callback(callback: Callable[..., Any], *args: Any) -> None:
	"""Call a callback that can be either sync or async"""
	result = callback(*args)
	if asyncio.iscoroutine(result):
		await result


def remote_execute(
	BROWSER_USE_API_KEY: str | None = None,
	server_url: str | None = None,
	log_level: str = 'INFO',
	quiet: bool = False,
	on_browser_created: Callable[[BrowserCreatedData], None]
	| Callable[[BrowserCreatedData], Coroutine[Any, Any, None]]
	| None = None,
	on_instance_ready: Callable[[], None] | Callable[[], Coroutine[Any, Any, None]] | None = None,
	on_log: Callable[[LogData], None] | Callable[[LogData], Coroutine[Any, Any, None]] | None = None,
	on_result: Callable[[ResultData], None] | Callable[[ResultData], Coroutine[Any, Any, None]] | None = None,
	on_error: Callable[[ErrorData], None] | Callable[[ErrorData], Coroutine[Any, Any, None]] | None = None,
	**env_vars: str,
):
	"""Decorator to execute browser automation code remotely.

	Args:
	    BROWSER_USE_API_KEY: Browser-Use API key (defaults to BROWSER_USE_API_KEY env var)
	    server_url: Remote execution server URL (defaults to localhost:8080)
	    log_level: Logging level for remote execution. Options:
	        - "INFO" (default): Shows important logs from browser-use agent
	        - "DEBUG": Shows all debug logs including internal browser operations
	        - "WARNING": Shows only warnings and errors
	        - "ERROR": Shows only errors
	    quiet: If True, suppresses all console output (live URL, logs, credits, etc.). Errors are still raised.
	    on_browser_created: Callback (sync or async) when browser session is created. Receives BrowserCreatedData with live_url.
	    on_instance_ready: Callback (sync or async) when instance is ready for execution.
	    on_log: Callback (sync or async) for log events. Receives LogData.
	    on_result: Callback (sync or async) when execution completes. Receives ResultData.
	    on_error: Callback (sync or async) for error events. Receives ErrorData.
	    **env_vars: Additional environment variables to pass to remote execution

	Example:
	    @remote_execute(
	        log_level="DEBUG",
	        on_browser_created=lambda data: print(f"Watch browser: {data.live_url}")
	    )
	    async def my_task(browser: Browser) -> str:
	        page = await browser.get_current_page()
	        await page.goto("https://example.com")
	        return await page.get_title()

	Event Callbacks Example:
	    def show_live_url(data: BrowserCreatedData):
	        print(f"Browser session: {data.session_id}")
	        print(f"Live URL: {data.live_url}")

	    @remote_execute(on_browser_created=show_live_url)
	    async def my_task(browser: Browser):
	        ...
	"""

	def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
		# Validate function signature - must have exactly 1 parameter: browser: Browser
		sig = inspect.signature(func)
		params = list(sig.parameters.values())

		if len(params) != 1:
			raise TypeError(f'{func.__name__}() must have exactly 1 parameter (browser: Browser), got {len(params)} parameters')

		param = params[0]
		if param.name != 'browser':
			raise TypeError(f'{func.__name__}() parameter must be named "browser", got "{param.name}"')

		# Check type annotation if present
		if param.annotation != inspect.Parameter.empty:
			# Get the string representation of the annotation
			annotation_str = str(param.annotation)
			# Accept Browser, BrowserSession, or any annotation containing 'Browser'
			if 'Browser' not in annotation_str:
				raise TypeError(f'{func.__name__}() parameter must be typed as Browser, got {annotation_str}')

		@wraps(func)
		async def wrapper(*args, **kwargs) -> T:
			# Validate no args/kwargs are passed (browser is provided by remote executor)
			if args or kwargs:
				raise TypeError(
					f'{func.__name__}() takes no arguments (browser is provided by remote executor), '
					f'but {len(args)} positional and {len(kwargs)} keyword arguments were given'
				)

			# Get API key
			api_key = BROWSER_USE_API_KEY or os.getenv('BROWSER_USE_API_KEY')
			if not api_key:
				raise RemoteExecutionError('BROWSER_USE_API_KEY is required')

			# Extract function source and create execution code
			source = inspect.getsource(func)
			tree = ast.parse(source)

			# Find and clean the function
			for node in tree.body:
				if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
					node.decorator_list = []  # Remove decorators
					break

			# Get source before transformation for extraction
			cleaned_source_before_transform = ast.unparse(tree)

			# Extract imports and classes used in the function BEFORE transformation
			# This gives us the AST nodes that we can then transform
			local_classes = _extract_local_classes_from_source(func, cleaned_source_before_transform)

			# Transform nonlocal statements into closure-friendly dict patterns
			# This fixes issues with nested functions that use nonlocal variables
			# Apply transformation to BOTH the main function tree AND extracted classes
			transformer = NonlocalToClosureTransformer()
			tree = transformer.transform(tree)

			# Also transform any extracted local classes/functions
			for i, cls_node in enumerate(local_classes):
				# Create a new transformer instance for each class to avoid state issues
				cls_transformer = NonlocalToClosureTransformer()
				# Wrap each class/function in a Module to transform it
				temp_tree = ast.Module(body=[cls_node], type_ignores=[])
				transformed_temp = cls_transformer.transform(temp_tree)
				local_classes[i] = transformed_temp.body[0]

			# Use cleaned source (without decorators and with transformations) for extraction
			cleaned_source = ast.unparse(tree)

			# Get combined source (function + classes) for import detection
			class_sources = [ast.unparse(cls) for cls in local_classes]
			combined_source = cleaned_source + '\n'.join(class_sources)
			used_imports = _extract_used_imports(func, combined_source)

			# Add necessary imports
			imports = [
				ast.ImportFrom(module='browser_use', names=[ast.alias(name='Browser')], level=0),
				ast.ImportFrom(module='browser_use', names=[ast.alias(name='Agent')], level=0),
			]

			# Add extracted imports
			imports.extend(used_imports)

			function_code = ast.unparse(ast.Module(body=imports + local_classes + tree.body, type_ignores=[]))

			# Debug: Check if nonlocal is still in the code
			if not quiet and 'nonlocal' in function_code:
				print("\n⚠️  WARNING: 'nonlocal' found in transformed code!", file=sys.stderr)
				print(f'Checking for _closure_vars: {"_closure_vars" in function_code}', file=sys.stderr)
				print(f'Number of local_classes extracted: {len(local_classes)}', file=sys.stderr)

				# Check each component
				main_func_has_nonlocal = 'nonlocal' in cleaned_source
				print(f'Main function has nonlocal: {main_func_has_nonlocal}', file=sys.stderr)

				for idx, cls in enumerate(local_classes):
					cls_src = ast.unparse(cls)
					if 'nonlocal' in cls_src:
						print(f'Local class/func {idx} ({cls.name}) has nonlocal!', file=sys.stderr)

				# Find the line with nonlocal
				for i, line in enumerate(function_code.split('\n')):
					if 'nonlocal' in line:
						print(f'Line {i + 1}: {line}', file=sys.stderr)

			# Create execution wrapper that expects browser parameter
			# The executor now captures the return value directly, no need for RESULT: marker
			execution_code = f"""
{function_code}

async def runner(browser):
    \"\"\"Wrapper function that calls the user function with browser\"\"\"
    result = await {func.__name__}(browser)
    return result
"""

			# Send to server
			payload: dict[str, Any] = {
				'code': base64.b64encode(execution_code.encode()).decode(),
			}
			# Combine env_vars with LOG_LEVEL
			combined_env: dict[str, str] = env_vars.copy() if env_vars else {}
			combined_env['LOG_LEVEL'] = log_level.upper()
			payload['env'] = combined_env

			# Use custom server URL or default to localhost with SSE streaming endpoint
			url = server_url or 'https://remote.api.browser-use.com/remote-execute-stream'

			# Headers with API key
			headers = {
				'X-API-Key': api_key,
			}

			# Handle SSE streaming
			# Use sentinel to distinguish "no result received" from "result is None"
			_NO_RESULT = object()
			execution_result = _NO_RESULT
			live_url_shown = False
			execution_started = False

			async with httpx.AsyncClient(timeout=1800.0) as client:
				async with client.stream('POST', url, json=payload, headers=headers) as response:
					response.raise_for_status()

					# Parse SSE events line by line
					async for line in response.aiter_lines():
						if not line or not line.startswith('data: '):
							continue

						# Extract JSON from "data: {...}"
						event_json = line[6:]  # Remove "data: " prefix
						try:
							# Parse into type-safe event model
							event = SSEEvent.from_json(event_json)

							# Handle different event types with type safety
							if event.type == SSEEventType.BROWSER_CREATED:
								# Type-safe access to BrowserCreatedData
								assert isinstance(event.data, BrowserCreatedData)

								# Call user callback if provided
								if on_browser_created:
									try:
										await _call_callback(on_browser_created, event.data)
									except Exception as e:
										if not quiet:
											print(f'⚠️  Error in on_browser_created callback: {e}')

								# Show live URL in console (unless user wants to handle it or quiet mode is on)
								if not quiet and event.data.live_url and not live_url_shown:
									width = get_terminal_width()
									print('\n' + '━' * width)
									print('👁️  LIVE BROWSER VIEW (Click to watch)')
									print(f'🔗 {event.data.live_url}')
									print('━' * width)
									live_url_shown = True

							elif event.type == SSEEventType.LOG:
								# Type-safe access to LogData
								assert isinstance(event.data, LogData)
								message = event.data.message
								level = event.data.level

								# Call user callback if provided
								if on_log:
									try:
										await _call_callback(on_log, event.data)
									except Exception as e:
										if not quiet:
											print(f'⚠️  Error in on_log callback: {e}')

								# Handle different log levels
								if level == 'stdout':
									# Print stdout with prefix to show it's from remote execution
									if not quiet:
										if not execution_started:
											width = get_terminal_width()
											print('\n' + '─' * width)
											print('⚡ Runtime Output')
											print('─' * width)
											execution_started = True
										print(f'  {message}', end='')
								elif level == 'stderr':
									# Print stderr with prefix
									if not quiet:
										if not execution_started:
											width = get_terminal_width()
											print('\n' + '─' * width)
											print('⚡ Runtime Output')
											print('─' * width)
											execution_started = True
										print(
											f'⚠️  {message}',
											end='',
											file=sys.stderr,
										)
								elif level == 'info':
									# System info messages (credit check, etc)
									if not quiet:
										if 'credit' in message.lower():
											# Extract amount from message
											import re

											match = re.search(r'\$[\d,]+\.?\d*', message)
											if match:
												print(f'💰 You have {match.group()} credits')
										else:
											print(f'ℹ️  {message}')
								else:
									if not quiet:
										print(f'  {message}')

							elif event.type == SSEEventType.INSTANCE_READY:
								# Call user callback if provided
								if on_instance_ready:
									try:
										await _call_callback(on_instance_ready)
									except Exception as e:
										if not quiet:
											print(f'⚠️  Error in on_instance_ready callback: {e}')

								# Print separator before execution starts
								if not quiet:
									print('✅ Browser ready, starting execution...\n')

							elif event.type == SSEEventType.RESULT:
								# Type-safe access to ResultData
								assert isinstance(event.data, ResultData)
								exec_response = event.data.execution_response

								# Call user callback if provided
								if on_result:
									try:
										await _call_callback(on_result, event.data)
									except Exception as e:
										if not quiet:
											print(f'⚠️  Error in on_result callback: {e}')

								if exec_response.success:
									# The result is now in the 'result' field, not stdout
									execution_result = exec_response.result
									# Print closing separator with spacing
									if not quiet and execution_started:
										width = get_terminal_width()
										print('\n' + '─' * width)
										print()  # Extra newline for spacing
								else:
									error_msg = exec_response.error or 'Unknown error'
									raise RemoteExecutionError(f'Execution failed: {error_msg}')

							elif event.type == SSEEventType.ERROR:
								# Type-safe access to ErrorData
								assert isinstance(event.data, ErrorData)

								# Call user callback if provided
								if on_error:
									try:
										await _call_callback(on_error, event.data)
									except Exception as e:
										if not quiet:
											print(f'⚠️  Error in on_error callback: {e}')

								raise RemoteExecutionError(f'Execution failed: {event.data.error}')

						except (json.JSONDecodeError, ValueError) as e:
							# Skip malformed events
							continue

			# Reconstruct based on return type annotation
			if execution_result is not _NO_RESULT:
				# We received a result (even if it's None)
				return_annotation = func.__annotations__.get('return')
				if return_annotation:
					parsed_result = _parse_with_type_annotation(execution_result, return_annotation)
					return parsed_result
				return execution_result  # type: ignore[return-value]

			raise RemoteExecutionError('No result received from execution')

		# Preserve type info
		wrapper.__annotations__ = func.__annotations__.copy()
		if 'browser' in wrapper.__annotations__:
			del wrapper.__annotations__['browser']

		# Update signature to remove browser parameter
		sig = inspect.signature(func)
		params = [p for p in sig.parameters.values() if p.name != 'browser']
		wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]

		return cast(Callable[..., Coroutine[Any, Any, T]], wrapper)

	return decorator


def _parse_with_type_annotation(data: Any, annotation: Any) -> Any:
	"""Parse data with type annotation (FastAPI-style parsing)

	This function recursively reconstructs Pydantic models from serialized data,
	handling nested structures properly.
	"""
	try:
		# Handle None
		if data is None:
			return None

		# Get origin and args for generic types
		origin = get_origin(annotation)
		args = get_args(annotation)

		# Handle Union types (both typing.Union and | syntax)
		if origin is Union or (hasattr(annotation, '__class__') and annotation.__class__.__name__ == 'UnionType'):
			# Try each union member until one works
			union_args = args or getattr(annotation, '__args__', [])
			for arg in union_args:
				if arg is type(None) and data is None:
					return None
				if arg is not type(None):
					try:
						return _parse_with_type_annotation(data, arg)
					except Exception:
						continue
			return data

		# Handle List types - recursively parse elements
		if origin is list:
			if not isinstance(data, list):
				return data
			if args:
				return [_parse_with_type_annotation(item, args[0]) for item in data]
			return data

		# Handle Dict types - recursively parse keys and values
		if origin is dict:
			if not isinstance(data, dict):
				return data
			if len(args) == 2:
				# Dict[key_type, value_type]
				return {_parse_with_type_annotation(k, args[0]): _parse_with_type_annotation(v, args[1]) for k, v in data.items()}
			return data

		# Handle Optional (which is Union[T, None])
		if hasattr(annotation, '__origin__') and annotation.__origin__ is Union:
			union_args = annotation.__args__
			if len(union_args) == 2 and type(None) in union_args:
				# This is Optional[T]
				non_none_type = union_args[0] if union_args[1] is type(None) else union_args[1]
				return _parse_with_type_annotation(data, non_none_type)

		# Handle Pydantic v2 models (model_validate method)
		if hasattr(annotation, 'model_validate'):
			try:
				# First try with strict validation
				return annotation.model_validate(data)
			except Exception:
				# If strict validation fails, try to recursively reconstruct nested models
				# This handles cases where nested Pydantic models have extra fields
				try:
					if hasattr(annotation, 'model_fields') and isinstance(data, dict):
						# Recursively parse nested Pydantic models
						parsed_data = {}
						for field_name, field_info in annotation.model_fields.items():
							if field_name in data:
								field_type = field_info.annotation
								parsed_data[field_name] = _parse_with_type_annotation(data[field_name], field_type)
						# Use model_construct to bypass validation
						if hasattr(annotation, 'model_construct'):
							return annotation.model_construct(**parsed_data)
				except Exception:
					pass

		# Handle Pydantic v1 models (parse_obj method)
		if hasattr(annotation, 'parse_obj'):
			try:
				return annotation.parse_obj(data)
			except Exception:
				# Try construct method for Pydantic v1 with recursive parsing
				try:
					if hasattr(annotation, '__fields__') and isinstance(data, dict):
						# Recursively parse nested Pydantic models (v1)
						parsed_data = {}
						for field_name, field_info in annotation.__fields__.items():
							if field_name in data:
								field_type = field_info.outer_type_
								parsed_data[field_name] = _parse_with_type_annotation(data[field_name], field_type)
						if hasattr(annotation, 'construct'):
							return annotation.construct(**parsed_data)
				except Exception:
					pass

		# Handle regular classes with constructor (fallback)
		if inspect.isclass(annotation) and isinstance(data, dict):
			try:
				return annotation(**data)
			except Exception:
				pass

		# Return as-is for basic types
		return data

	except Exception:
		# If parsing fails, return original data
		return data


def _extract_local_classes_from_source(func: Callable, cleaned_source: str) -> list:
	"""Extract local class and function definitions used by the function

	Args:
	    func: The function being decorated
	    cleaned_source: The source code without decorators
	"""
	definitions = []

	# List of known external packages to skip
	external_packages = {
		'browser_use',
		'pydantic',
		'typing',
		'requests',
		'httpx',
		'json',
		'datetime',
		'asyncio',
		'os',
		'sys',
		're',
		'time',
		'collections',
		'itertools',
		'functools',
		'base64',
		'logging',
		'numpy',
		'pandas',
		'beautifulsoup4',
		'bs4',
		'scrapy',
		'lxml',
	}

	# Get the module where the function is defined
	module = inspect.getmodule(func)
	if not module:
		return definitions

	# Use the cleaned source (without decorators) to find referenced names
	source = cleaned_source

	# Get the main function name to exclude it from extraction
	main_func_name = func.__name__

	# Parse the entire module to find definitions and imports
	try:
		module_source = inspect.getsource(module)
		module_tree = ast.parse(module_source)

		# Find all TOP-LEVEL class and function definitions in the current module
		# Use module_tree.body instead of ast.walk() to avoid extracting nested functions
		for node in module_tree.body:
			if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
				# Skip the main function itself
				if node.name == main_func_name:
					continue

				# Check if this definition is referenced in the function
				if node.name in source:
					# Strip decorators from extracted definitions
					node.decorator_list = []
					definitions.append(node)

		# Handle local imports like "from views import Result"
		import_files = []
		for node in module_tree.body:  # Only check top-level imports
			if isinstance(node, ast.ImportFrom):
				# Skip if it's a relative import or external package
				if node.level > 0 or not node.module:
					continue

				package_name = node.module.split('.')[0]
				if package_name in external_packages:
					continue

				# Try to find the module file in the same directory
				try:
					if not module.__file__:
						continue
					module_dir = os.path.dirname(module.__file__)
					import_file = os.path.join(module_dir, f'{node.module}.py')
					if os.path.exists(import_file):
						import_files.append((import_file, [alias.name for alias in node.names]))
				except Exception:
					pass

		# Extract definitions from imported local files
		for import_file, imported_names in import_files:
			try:
				with open(import_file) as f:
					imported_source = f.read()
				imported_tree = ast.parse(imported_source)

				# Only extract TOP-LEVEL definitions from imported files
				for node in imported_tree.body:
					if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
						# Include if explicitly imported or used in source
						if node.name in imported_names or node.name in source:
							# Strip decorators from imported definitions too
							node.decorator_list = []
							definitions.append(node)

			except Exception:
				pass

	except Exception:
		pass

	return definitions


def _extract_used_imports(func: Callable, source: str) -> list:
	"""Extract all imports from the function's module that are used in the function"""
	imports = []

	# List of known safe packages that should be imported
	# These are packages that are likely to be installed in the remote environment
	safe_packages = {
		'browser_use',
		'pydantic',
		'typing',
		'requests',
		'httpx',
		'json',
		'datetime',
		'asyncio',
		'os',
		'sys',
		're',
		'time',
		'collections',
		'itertools',
		'functools',
		'base64',
		'logging',
		'numpy',
		'pandas',
		'beautifulsoup4',
		'bs4',
		'scrapy',
		'lxml',
	}

	# Get the module where the function is defined
	module = inspect.getmodule(func)
	if not module:
		return imports

	try:
		# Parse the module source to extract imports
		module_source = inspect.getsource(module)
		module_tree = ast.parse(module_source)

		# Extract all imports and check if they're used in the function
		for node in ast.walk(module_tree):
			if isinstance(node, ast.Import):
				# Handle: import xxx
				for alias in node.names:
					package_name = alias.name.split('.')[0]  # Get root package
					if (alias.name in source or (alias.asname and alias.asname in source)) and package_name in safe_packages:
						imports.append(node)
						break
			elif isinstance(node, ast.ImportFrom):
				# Skip relative imports (level > 0 means relative)
				if node.level > 0:
					continue

				# Skip local module imports
				if node.module:
					package_name = node.module.split('.')[0]
					if package_name not in safe_packages:
						continue

				# Handle: from xxx import yyy
				# Check if any imported names are used
				used_names = []
				for alias in node.names:
					name_to_check = alias.asname if alias.asname else alias.name
					if name_to_check in source:
						used_names.append(alias)

				if used_names and node.module:
					# Create a new ImportFrom with only the used names
					imports.append(
						ast.ImportFrom(
							module=node.module,
							names=used_names,
							level=0,  # Always use absolute imports
						)
					)
	except Exception:
		# If we can't parse the module, continue without imports
		pass

	return imports
