import ast
import base64
import inspect
import json
import os
import sys
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar, Union, cast, get_args, get_origin

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


def get_terminal_width() -> int:
	"""Get terminal width, default to 80 if unable to detect"""
	try:
		return os.get_terminal_size().columns
	except (AttributeError, OSError):
		return 80


def remote_execute(
	BROWSER_USE_API_KEY: str | None = None,
	server_url: str | None = None,
	log_level: str = 'INFO',
	on_browser_created: Callable[[BrowserCreatedData], None] | None = None,
	on_instance_ready: Callable[[], None] | None = None,
	on_log: Callable[[LogData], None] | None = None,
	on_result: Callable[[ResultData], None] | None = None,
	on_error: Callable[[ErrorData], None] | None = None,
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
	    on_browser_created: Callback when browser session is created. Receives BrowserCreatedData with live_url.
	    on_instance_ready: Callback when instance is ready for execution.
	    on_log: Callback for log events. Receives LogData.
	    on_result: Callback when execution completes. Receives ResultData.
	    on_error: Callback for error events. Receives ErrorData.
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
		@wraps(func)
		async def wrapper(*args, **kwargs) -> T:
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

			# Use cleaned source (without decorators) for extraction
			cleaned_source = ast.unparse(tree)

			# Extract imports and classes used in the function
			local_classes = _extract_local_classes_from_source(func, cleaned_source)

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
										on_browser_created(event.data)
									except Exception as e:
										print(f'⚠️  Error in on_browser_created callback: {e}')

								# Show live URL in console (unless user wants to handle it)
								if event.data.live_url and not live_url_shown:
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
										on_log(event.data)
									except Exception as e:
										print(f'⚠️  Error in on_log callback: {e}')

								# Handle different log levels
								if level == 'stdout':
									# Print stdout with prefix to show it's from remote execution
									if not execution_started:
										width = get_terminal_width()
										print('\n' + '─' * width)
										print('⚡ Runtime Output')
										print('─' * width)
										execution_started = True
									print(f'  {message}', end='')
								elif level == 'stderr':
									# Print stderr with prefix
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
									if 'credit' in message.lower():
										# Extract amount from message
										import re

										match = re.search(r'\$[\d,]+\.?\d*', message)
										if match:
											print(f'💰 You have {match.group()} credits')
									else:
										print(f'ℹ️  {message}')
								else:
									print(f'  {message}')

							elif event.type == SSEEventType.INSTANCE_READY:
								# Call user callback if provided
								if on_instance_ready:
									try:
										on_instance_ready()
									except Exception as e:
										print(f'⚠️  Error in on_instance_ready callback: {e}')

								# Print separator before execution starts
								print('✅ Browser ready, starting execution...\n')

							elif event.type == SSEEventType.RESULT:
								# Type-safe access to ResultData
								assert isinstance(event.data, ResultData)
								exec_response = event.data.execution_response

								# Call user callback if provided
								if on_result:
									try:
										on_result(event.data)
									except Exception as e:
										print(f'⚠️  Error in on_result callback: {e}')

								if exec_response.success:
									# The result is now in the 'result' field, not stdout
									execution_result = exec_response.result
									# Print closing separator with spacing
									if execution_started:
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
										on_error(event.data)
									except Exception as e:
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

		# Find all class and function definitions in the current module
		for node in ast.walk(module_tree):
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
				with open(import_file, 'r') as f:
					imported_source = f.read()
				imported_tree = ast.parse(imported_source)

				for node in ast.walk(imported_tree):
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
