"""Element-focused text input behavior for default browser actions."""

import asyncio
import json

from browser_use.browser.events import TypeTextEvent
from browser_use.browser.views import BrowserError
from browser_use.browser.watchdogs.click_actions import ClickActions
from browser_use.browser.watchdogs.keyboard_actions import KeyboardActions
from browser_use.dom.service import EnhancedDOMTreeNode


class TextInputActions:
	"""Focus, clear, populate, and verify text-bearing DOM elements."""

	def __init__(
		self,
		browser_session,
		click_actions: ClickActions,
		keyboard_actions: KeyboardActions,
	) -> None:
		self.browser_session = browser_session
		self.click_actions = click_actions
		self.keyboard_actions = keyboard_actions

	@property
	def logger(self):
		return self.browser_session.logger

	async def handle_type_text(self, event: TypeTextEvent) -> dict | None:
		"""Handle text input request with CDP."""
		try:
			# Use the provided node
			element_node = event.node
			index_for_logging = self.browser_session.dom_state.get_selector_index(element_node)

			# Check if this is index 0 or a falsy index - type to the page (whatever has focus)
			if not element_node.backend_node_id or element_node.backend_node_id == 0:
				# Type to the page without focusing any specific element
				await self.keyboard_actions.type_to_page(event.text)
				# Log with sensitive data protection
				if event.is_sensitive:
					if event.sensitive_key_name:
						self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> to the page (current focus)')
					else:
						self.logger.info('⌨️ Typed <sensitive> to the page (current focus)')
				else:
					self.logger.info(f'⌨️ Typed "{event.text}" to the page (current focus)')
				return None  # No coordinates available for page typing
			else:
				try:
					# Try to type to the specific element
					input_metadata = await self._input_text_element_node_impl(
						element_node,
						event.text,
						clear=event.clear or (not event.text),
						is_sensitive=event.is_sensitive,
					)
					# Log with sensitive data protection
					if event.is_sensitive:
						if event.sensitive_key_name:
							self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> into element with index {index_for_logging}')
						else:
							self.logger.info(f'⌨️ Typed <sensitive> into element with index {index_for_logging}')
					else:
						self.logger.info(f'⌨️ Typed "{event.text}" into element with index {index_for_logging}')
					self.logger.debug(f'Element xpath: {element_node.xpath}')
					return input_metadata  # Return coordinates if available
				except Exception as e:
					# Element not found or error - fall back to typing to the page
					self.logger.warning(f'Failed to type to element {index_for_logging}: {e}. Falling back to page typing.')
					try:
						await asyncio.wait_for(self.click_actions.click_element(element_node), timeout=10.0)
					except Exception as e:
						self.logger.debug(f'Fallback click for page typing failed: {e}')
					await self.keyboard_actions.type_to_page(event.text)
					# Log with sensitive data protection
					if event.is_sensitive:
						if event.sensitive_key_name:
							self.logger.info(f'⌨️ Typed <{event.sensitive_key_name}> to the page as fallback')
						else:
							self.logger.info('⌨️ Typed <sensitive> to the page as fallback')
					else:
						self.logger.info(f'⌨️ Typed "{event.text}" to the page as fallback')
					return None  # No coordinates available for fallback typing

			# Note: We don't clear cached state here - let multi_act handle DOM change detection
			# by explicitly rebuilding and comparing when needed
		except Exception as e:
			raise

	async def _clear_text_field(self, object_id: str, cdp_session) -> bool:
		"""Clear text field using multiple strategies, starting with the most reliable."""
		try:
			# Strategy 1: Direct JavaScript value/content setting (handles both inputs and contenteditable)
			self.logger.debug('🧹 Clearing text field using JavaScript value setting')

			clear_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': """
						function() {
							// Check if it's a contenteditable element
							const hasContentEditable = this.getAttribute('contenteditable') === 'true' ||
													this.getAttribute('contenteditable') === '' ||
													this.isContentEditable === true;

							if (hasContentEditable) {
								// For contenteditable elements, clear all content
								while (this.firstChild) {
									this.removeChild(this.firstChild);
								}
								this.textContent = "";
								this.innerHTML = "";

								// Focus and position cursor at the beginning
								this.focus();
								const selection = window.getSelection();
								const range = document.createRange();
								range.setStart(this, 0);
								range.setEnd(this, 0);
								selection.removeAllRanges();
								selection.addRange(range);

								// Dispatch events
								this.dispatchEvent(new Event("input", { bubbles: true }));
								this.dispatchEvent(new Event("change", { bubbles: true }));

								return {cleared: true, method: 'contenteditable', finalText: this.textContent};
							} else if (this.value !== undefined) {
								// For regular inputs with value property
								try {
									this.select();
								} catch (e) {
									// ignore
								}
								// For real <input>/<textarea>, use the prototype's native value setter:
								// React patches the instance setter to track values, so a direct
								// `this.value = ""` makes React's tracker think nothing changed and the
								// input event gets ignored, leaving controlled components with the old
								// value. For anything else with a .value accessor (web components,
								// selects), the prototype setter throws Illegal invocation - keep using
								// the element's own setter there.
								if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement) {
									const proto = this instanceof HTMLTextAreaElement
										? window.HTMLTextAreaElement.prototype
										: window.HTMLInputElement.prototype;
									const desc = Object.getOwnPropertyDescriptor(proto, 'value');
									try {
										desc.set.call(this, "");
									} catch (e) {
										this.value = "";
									}
								} else {
									this.value = "";
								}
								this.dispatchEvent(new Event("input", { bubbles: true }));
								this.dispatchEvent(new Event("change", { bubbles: true }));
								return {cleared: true, method: 'value', finalText: this.value};
							} else {
								return {cleared: false, method: 'none', error: 'Not a supported input type'};
							}
						}
					""",
					'objectId': object_id,
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			)

			# Check the clear result
			clear_info = clear_result.get('result', {}).get('value', {})
			self.logger.debug(f'Clear result: {clear_info}')

			if clear_info.get('cleared'):
				final_text = clear_info.get('finalText', '')
				if not final_text or not final_text.strip():
					self.logger.debug(f'✅ Text field cleared successfully using {clear_info.get("method")}')
					return True
				else:
					self.logger.debug(f'⚠️ JavaScript clear partially failed, field still contains: "{final_text}"')
			else:
				self.logger.debug(f'❌ JavaScript clear failed: {clear_info.get("error", "Unknown error")}')

		except Exception as e:
			self.logger.debug(f'JavaScript clear failed with exception: {e}')
			return False

		# Strategy 2: Triple-click + Delete (fallback for stubborn fields)
		try:
			self.logger.debug('🧹 Fallback: Clearing using triple-click + Delete')

			# Get element center coordinates for triple-click
			bounds_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': 'function() { return this.getBoundingClientRect(); }',
					'objectId': object_id,
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			)

			if bounds_result.get('result', {}).get('value'):
				bounds = bounds_result['result']['value']
				center_x = bounds['x'] + bounds['width'] / 2
				center_y = bounds['y'] + bounds['height'] / 2

				# Triple-click to select all text
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mousePressed',
						'x': center_x,
						'y': center_y,
						'button': 'left',
						'clickCount': 3,
					},
					session_id=cdp_session.session_id,
				)
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseReleased',
						'x': center_x,
						'y': center_y,
						'button': 'left',
						'clickCount': 3,
					},
					session_id=cdp_session.session_id,
				)

				# Delete selected text
				await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
					params={
						'type': 'keyDown',
						'key': 'Delete',
						'code': 'Delete',
					},
					session_id=cdp_session.session_id,
				)
				await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
					params={
						'type': 'keyUp',
						'key': 'Delete',
						'code': 'Delete',
					},
					session_id=cdp_session.session_id,
				)

				self.logger.debug('✅ Text field cleared using triple-click + Delete')
				return True

		except Exception as e:
			self.logger.debug(f'Triple-click clear failed: {e}')

		# Strategy 3: Keyboard shortcuts (last resort)
		try:
			import platform

			is_macos = platform.system() == 'Darwin'
			select_all_modifier = 4 if is_macos else 2  # Meta=4 (Cmd), Ctrl=2
			modifier_name = 'Cmd' if is_macos else 'Ctrl'

			self.logger.debug(f'🧹 Last resort: Clearing using {modifier_name}+A + Backspace')

			# Select all text (Ctrl/Cmd+A)
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyDown',
					'key': 'a',
					'code': 'KeyA',
					'modifiers': select_all_modifier,
				},
				session_id=cdp_session.session_id,
			)
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyUp',
					'key': 'a',
					'code': 'KeyA',
					'modifiers': select_all_modifier,
				},
				session_id=cdp_session.session_id,
			)

			# Delete selected text (Backspace)
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyDown',
					'key': 'Backspace',
					'code': 'Backspace',
				},
				session_id=cdp_session.session_id,
			)
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={
					'type': 'keyUp',
					'key': 'Backspace',
					'code': 'Backspace',
				},
				session_id=cdp_session.session_id,
			)

			self.logger.debug('✅ Text field cleared using keyboard shortcuts')
			return True

		except Exception as e:
			self.logger.debug(f'All clearing strategies failed: {e}')
			return False

	async def _focus_element_simple(
		self, backend_node_id: int, object_id: str, cdp_session, input_coordinates: dict | None = None
	) -> bool:
		"""Simple focus strategy: CDP first, then click if failed."""

		# Strategy 1: Try CDP DOM.focus first
		try:
			result = await cdp_session.cdp_client.send.DOM.focus(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_session.session_id,
			)
			self.logger.debug(f'Element focused using CDP DOM.focus (result: {result})')
			return True

		except Exception as e:
			self.logger.debug(f'❌ CDP DOM.focus threw exception: {type(e).__name__}: {e}')

		# Strategy 2: Try click to focus if CDP failed
		if input_coordinates and 'input_x' in input_coordinates and 'input_y' in input_coordinates:
			try:
				click_x = input_coordinates['input_x']
				click_y = input_coordinates['input_y']

				self.logger.debug(f'🎯 Attempting click-to-focus at ({click_x:.1f}, {click_y:.1f})')

				# Click to focus
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mousePressed',
						'x': click_x,
						'y': click_y,
						'button': 'left',
						'clickCount': 1,
					},
					session_id=cdp_session.session_id,
				)
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseReleased',
						'x': click_x,
						'y': click_y,
						'button': 'left',
						'clickCount': 1,
					},
					session_id=cdp_session.session_id,
				)

				self.logger.debug('✅ Element focused using click method')
				return True

			except Exception as e:
				self.logger.debug(f'Click focus failed: {e}')

		# Both strategies failed
		self.logger.debug('Focus strategies failed, will attempt typing anyway')
		return False

	def _requires_direct_value_assignment(self, element_node: EnhancedDOMTreeNode) -> bool:
		"""
		Check if an element requires direct value assignment instead of character-by-character typing.

		Certain input types have compound components, custom plugins, or special requirements
		that make character-by-character typing unreliable. These need direct .value assignment:

		Native HTML5:
		- date, time, datetime-local: Have spinbutton components (ISO format required)
		- month, week: Similar compound structure
		- color: Expects hex format #RRGGBB
		- range: Needs numeric value within min/max

		jQuery/Bootstrap Datepickers:
		- Detected by class names or data attributes
		- Often expect specific date formats (MM/DD/YYYY, DD/MM/YYYY, etc.)

		Note: We use direct assignment because:
		1. Typing triggers intermediate validation that might reject partial values
		2. Compound components (like date spinbuttons) don't work with sequential typing
		3. It's much faster and more reliable
		4. We dispatch proper input/change events afterward to trigger listeners
		"""
		if not element_node.tag_name or not element_node.attributes:
			return False

		tag_name = element_node.tag_name.lower()

		# Check for native HTML5 inputs that need direct assignment
		if tag_name == 'input':
			input_type = element_node.attributes.get('type', '').lower()

			# Native HTML5 inputs with compound components or strict formats
			if input_type in {'date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'}:
				return True

			# Detect jQuery/Bootstrap datepickers (text inputs with datepicker plugins)
			if input_type in {'text', ''}:
				# Check for common datepicker indicators
				class_attr = element_node.attributes.get('class', '').lower()
				if any(
					indicator in class_attr
					for indicator in ['datepicker', 'daterangepicker', 'datetimepicker', 'bootstrap-datepicker']
				):
					return True

				# Check for data attributes indicating datepickers
				if any(attr in element_node.attributes for attr in ['data-datepicker', 'data-date-format', 'data-provide']):
					return True

		return False

	async def _set_value_directly(self, element_node: EnhancedDOMTreeNode, text: str, object_id: str, cdp_session) -> None:
		"""
		Set element value directly using JavaScript for inputs that don't support typing.

		This is used for:
		- Date/time inputs where character-by-character typing doesn't work
		- jQuery datepickers that need direct value assignment
		- Color/range inputs that need specific formats
		- Any input with custom plugins that intercept typing

		After setting the value, we dispatch comprehensive events to ensure all frameworks
		and plugins recognize the change (React, Vue, Angular, jQuery, etc.)
		"""
		try:
			# Set the value using JavaScript with comprehensive event dispatching
			# callFunctionOn expects a function body (not a self-invoking function)
			set_value_js = f"""
			function() {{
				// Store old value for comparison
				const oldValue = this.value;

				// REACT-COMPATIBLE VALUE SETTING:
				// React uses Object.getOwnPropertyDescriptor to track input changes
				// We need to use the native setter to bypass React's tracking and then trigger events
				const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
					window.HTMLInputElement.prototype,
					'value'
				).set;

				// Set the value using the native setter (bypasses React's control)
				nativeInputValueSetter.call(this, {json.dumps(text)});

				// Dispatch comprehensive events to ensure all frameworks detect the change
				// Order matters: focus -> input -> change -> blur (mimics user interaction)

				// 1. Focus event (in case element isn't focused)
				this.dispatchEvent(new FocusEvent('focus', {{ bubbles: true }}));

				// 2. Input event (CRITICAL for React onChange)
				// React listens to 'input' events on the document and checks for value changes
				const inputEvent = new Event('input', {{ bubbles: true, cancelable: true }});
				this.dispatchEvent(inputEvent);

				// 3. Change event (for form handling, traditional listeners)
				const changeEvent = new Event('change', {{ bubbles: true, cancelable: true }});
				this.dispatchEvent(changeEvent);

				// 4. Blur event (triggers final validation in some libraries)
				this.dispatchEvent(new FocusEvent('blur', {{ bubbles: true }}));

				// 5. jQuery-specific events (if jQuery is present)
				if (typeof jQuery !== 'undefined' && jQuery.fn) {{
					try {{
						jQuery(this).trigger('change');
						// Trigger datepicker-specific events if it's a datepicker
						if (jQuery(this).data('datepicker')) {{
							jQuery(this).datepicker('update');
						}}
					}} catch (e) {{
						// jQuery not available or error, continue anyway
					}}
				}}

				return this.value;
			}}
			"""

			result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': object_id,
					'functionDeclaration': set_value_js,
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			)

			# Verify the value was set correctly
			if 'result' in result and 'value' in result['result']:
				actual_value = result['result']['value']
				self.logger.debug(f'✅ Value set directly to: "{actual_value}"')
			else:
				self.logger.warning('⚠️ Could not verify value was set correctly')

		except Exception as e:
			self.logger.error(f'❌ Failed to set value directly: {e}')
			raise

	async def _input_text_element_node_impl(
		self, element_node: EnhancedDOMTreeNode, text: str, clear: bool = True, is_sensitive: bool = False
	) -> dict | None:
		"""
		Input text into an element using pure CDP with improved focus fallbacks.

		For date/time inputs, uses direct value assignment instead of typing.
		"""

		try:
			# Get CDP client
			cdp_client = self.browser_session.cdp_client

			# Get the correct session ID for the element's iframe
			# session_id = await self._get_session_id_for_element(element_node)

			# cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=element_node.target_id, focus=True)
			cdp_session = await self.browser_session.session_manager.cdp_client_for_node(element_node)

			# Get element info
			backend_node_id = element_node.backend_node_id

			# Track coordinates for metadata
			input_coordinates = None

			# Scroll element into view
			try:
				await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
					params={'backendNodeId': backend_node_id}, session_id=cdp_session.session_id
				)
				await asyncio.sleep(0.01)
			except Exception as e:
				# Node detached errors are common with shadow DOM and dynamic content
				# The element can still be interacted with even if scrolling fails
				error_str = str(e)
				if 'Node is detached from document' in error_str or 'detached from document' in error_str:
					self.logger.debug(
						f'Element node temporarily detached during scroll (common with shadow DOM), continuing: {element_node}'
					)
				else:
					self.logger.debug(f'Failed to scroll element {element_node} into view before typing: {type(e).__name__}: {e}')

			# Get object ID for the element
			result = await cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_session.session_id,
			)
			assert 'object' in result and 'objectId' in result['object'], (
				'Failed to find DOM element based on backendNodeId, maybe page content changed?'
			)
			object_id = result['object']['objectId']

			# Get current coordinates using unified method
			coords = await self.browser_session.dom_state.get_element_coordinates(backend_node_id, cdp_session)
			if coords:
				center_x = coords.x + coords.width / 2
				center_y = coords.y + coords.height / 2

				# Check for occlusion before using coordinates for focus
				is_occluded = await self.click_actions.is_element_occluded(backend_node_id, center_x, center_y, cdp_session)

				if is_occluded:
					self.logger.debug('🚫 Input element is occluded, skipping coordinate-based focus')
					input_coordinates = None  # Force fallback to CDP-only focus
				else:
					input_coordinates = {'input_x': center_x, 'input_y': center_y}
					self.logger.debug(f'Using unified coordinates: x={center_x:.1f}, y={center_y:.1f}')
			else:
				input_coordinates = None
				self.logger.debug('No coordinates found for element')

			# Ensure we have a valid object_id before proceeding
			if not object_id:
				raise ValueError('Could not get object_id for element')

			# Step 1: Focus the element using simple strategy
			focused_successfully = await self._focus_element_simple(
				backend_node_id=backend_node_id, object_id=object_id, cdp_session=cdp_session, input_coordinates=input_coordinates
			)

			# Step 2: Check if this element requires direct value assignment (date/time inputs)
			requires_direct_assignment = self._requires_direct_value_assignment(element_node)

			if requires_direct_assignment:
				# Date/time inputs: use direct value assignment instead of typing
				self.logger.debug(
					f'🎯 Element type={element_node.attributes.get("type")} requires direct value assignment, setting value directly'
				)
				await self._set_value_directly(element_node, text, object_id, cdp_session)

				# Return input coordinates for metadata
				return input_coordinates

			# Step 3: Clear existing text if requested (only for regular inputs that support typing)
			if clear:
				cleared_successfully = await self._clear_text_field(object_id=object_id, cdp_session=cdp_session)
				if not cleared_successfully:
					self.logger.warning('⚠️ Text field clearing failed, typing may append to existing text')

			# Step 4: Type the text character by character using proper human-like key events
			# This emulates exactly how a human would type, which modern websites expect
			if is_sensitive:
				# Note: sensitive_key_name is not passed to this low-level method,
				# but we could extend the signature if needed for more granular logging
				self.logger.debug('🎯 Typing <sensitive> character by character')
			else:
				self.logger.debug(f'🎯 Typing text character by character: "{text}"')

			# Detect contenteditable elements (may have leaf-start bug where first char is dropped)
			_attrs = element_node.attributes or {}
			_is_contenteditable = _attrs.get('contenteditable') in ('true', '') or (
				_attrs.get('role') == 'textbox' and element_node.tag_name not in ('input', 'textarea')
			)

			# For contenteditable: after typing first char, check if dropped and retype if needed
			_check_first_char = _is_contenteditable and len(text) > 0 and clear
			_first_char = text[0] if _check_first_char else None

			for i, char in enumerate(text):
				# Handle newline characters as Enter key
				if char == '\n':
					# Send proper Enter key sequence
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_session.session_id,
					)

					# Small delay to emulate human typing speed
					await asyncio.sleep(0.001)

					# Send char event with carriage return
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': '\r',
							'key': 'Enter',
						},
						session_id=cdp_session.session_id,
					)

					# Send keyUp event
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': 'Enter',
							'code': 'Enter',
							'windowsVirtualKeyCode': 13,
						},
						session_id=cdp_session.session_id,
					)
				else:
					# Handle regular characters
					# Get proper modifiers, VK code, and base key for the character
					modifiers, vk_code, base_key = self.keyboard_actions.character_key_info(char)
					key_code = self.keyboard_actions.key_code_for_character(base_key)

					# self.logger.debug(f'🎯 Typing character {i + 1}/{len(text)}: "{char}" (base_key: {base_key}, code: {key_code}, modifiers: {modifiers}, vk: {vk_code})')

					# Step 1: Send keyDown event (NO text parameter)
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyDown',
							'key': base_key,
							'code': key_code,
							'modifiers': modifiers,
							'windowsVirtualKeyCode': vk_code,
						},
						session_id=cdp_session.session_id,
					)

					# Small delay to emulate human typing speed
					await asyncio.sleep(0.005)

					# Step 2: Send char event (WITH text parameter) - this is crucial for text input
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'char',
							'text': char,
							'key': char,
						},
						session_id=cdp_session.session_id,
					)

					# Step 3: Send keyUp event (NO text parameter)
					await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
						params={
							'type': 'keyUp',
							'key': base_key,
							'code': key_code,
							'modifiers': modifiers,
							'windowsVirtualKeyCode': vk_code,
						},
						session_id=cdp_session.session_id,
					)

				# After first char on contenteditable: check if dropped and retype if needed
				if i == 0 and _check_first_char and _first_char:
					check_result = await cdp_session.cdp_client.send.Runtime.evaluate(
						params={'expression': 'document.activeElement.textContent'},
						session_id=cdp_session.session_id,
					)
					content = check_result.get('result', {}).get('value', '')
					if _first_char not in content:
						self.logger.debug(f'🎯 First char "{_first_char}" was dropped (leaf-start bug), retyping')
						# Retype the first character - cursor now past leaf-start
						modifiers, vk_code, base_key = self.keyboard_actions.character_key_info(_first_char)
						key_code = self.keyboard_actions.key_code_for_character(base_key)
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyDown',
								'key': base_key,
								'code': key_code,
								'modifiers': modifiers,
								'windowsVirtualKeyCode': vk_code,
							},
							session_id=cdp_session.session_id,
						)
						await asyncio.sleep(0.005)
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={'type': 'char', 'text': _first_char, 'key': _first_char},
							session_id=cdp_session.session_id,
						)
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={
								'type': 'keyUp',
								'key': base_key,
								'code': key_code,
								'modifiers': modifiers,
								'windowsVirtualKeyCode': vk_code,
							},
							session_id=cdp_session.session_id,
						)

				# Small delay between characters to look human (realistic typing speed)
				await asyncio.sleep(0.001)

			# Step 4: Trigger framework-aware DOM events after typing completion
			# Modern JavaScript frameworks (React, Vue, Angular) rely on these events
			# to update their internal state and trigger re-renders
			await self._trigger_framework_events(object_id=object_id, cdp_session=cdp_session)

			# Step 5: Read back actual value for verification (skip for sensitive data)
			if not is_sensitive:
				try:
					await asyncio.sleep(0.05)  # let autocomplete/formatter JS settle
					readback_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'objectId': object_id,
							'functionDeclaration': 'function() { return this.value !== undefined ? this.value : this.textContent; }',
							'returnByValue': True,
						},
						session_id=cdp_session.session_id,
					)
					actual_value = readback_result.get('result', {}).get('value')
					if actual_value is not None:
						if input_coordinates is None:
							input_coordinates = {}
						input_coordinates['actual_value'] = actual_value
				except Exception as e:
					self.logger.debug(f'Value readback failed (non-critical): {e}')

			# Step 6: Auto-retry on concatenation mismatch (only when clear was requested)
			# If we asked to clear but the readback value contains the typed text as a substring
			# yet is longer, the field had pre-existing text that wasn't cleared. Set directly.
			if clear and not is_sensitive and input_coordinates and 'actual_value' in input_coordinates:
				actual_value = input_coordinates['actual_value']
				if (
					isinstance(actual_value, str)
					and actual_value != text
					and len(actual_value) > len(text)
					and (actual_value.endswith(text) or actual_value.startswith(text))
				):
					self.logger.info(f'🔄 Concatenation detected: got "{actual_value}", expected "{text}" — auto-retrying')
					try:
						# Clear + set value via native setter in one JS call (works with React/Vue)
						retry_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
							params={
								'objectId': object_id,
								'functionDeclaration': """
									function(newValue) {
										if (this.value !== undefined) {
											// Pick the descriptor for THIS element type; calling the
											// HTMLInputElement setter on a textarea or web component
											// throws Illegal invocation.
											var desc = null;
											if (this instanceof HTMLInputElement) {
												desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
											} else if (this instanceof HTMLTextAreaElement) {
												desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
											}
											if (desc && desc.set) {
												try {
													desc.set.call(this, newValue);
												} catch (e) {
													this.value = newValue;
												}
											} else {
												this.value = newValue;
											}
										} else if (this.isContentEditable) {
											this.textContent = newValue;
										}
										this.dispatchEvent(new Event('input', { bubbles: true }));
										this.dispatchEvent(new Event('change', { bubbles: true }));
										return this.value !== undefined ? this.value : this.textContent;
									}
								""",
								'arguments': [{'value': text}],
								'returnByValue': True,
							},
							session_id=cdp_session.session_id,
						)
						retry_value = retry_result.get('result', {}).get('value')
						if retry_value is not None:
							input_coordinates['actual_value'] = retry_value
							if retry_value == text:
								self.logger.info('✅ Auto-retry fixed concatenation')
							else:
								self.logger.warning(f'⚠️ Auto-retry value still differs: "{retry_value}"')
					except Exception as e:
						self.logger.debug(f'Auto-retry failed (non-critical): {e}')

			# Return coordinates metadata if available
			return input_coordinates

		except Exception as e:
			self.logger.error(f'Failed to input text via CDP: {type(e).__name__}: {e}')
			raise BrowserError(f'Failed to input text into element: {repr(element_node)}')

	async def _trigger_framework_events(self, object_id: str, cdp_session) -> None:
		"""
		Trigger framework-aware DOM events after text input completion.

		This is critical for modern JavaScript frameworks (React, Vue, Angular, etc.)
		that rely on DOM events to update their internal state and trigger re-renders.

		Args:
			object_id: CDP object ID of the input element
			cdp_session: CDP session for the element's context
		"""
		try:
			# Execute JavaScript to trigger comprehensive event sequence
			framework_events_script = """
			function() {
				// Find the target element (available as 'this' when using objectId)
				const element = this;
				if (!element) return false;

				// Ensure element is focused
				element.focus();

				// Comprehensive event sequence for maximum framework compatibility
				const events = [
					// Input event - primary event for React controlled components
					{ type: 'input', bubbles: true, cancelable: true },
					// Change event - important for form validation and Vue v-model
					{ type: 'change', bubbles: true, cancelable: true },
					// Blur event - triggers validation in many frameworks
					{ type: 'blur', bubbles: true, cancelable: true }
				];

				let success = true;

				events.forEach(eventConfig => {
					try {
						const event = new Event(eventConfig.type, {
							bubbles: eventConfig.bubbles,
							cancelable: eventConfig.cancelable
						});

						// Special handling for InputEvent (more specific than Event)
						if (eventConfig.type === 'input') {
							const inputEvent = new InputEvent('input', {
								bubbles: true,
								cancelable: true,
								data: element.value,
								inputType: 'insertText'
							});
							element.dispatchEvent(inputEvent);
						} else {
							element.dispatchEvent(event);
						}
					} catch (e) {
						success = false;
						console.warn('Framework event dispatch failed:', eventConfig.type, e);
					}
				});

				// Special React synthetic event handling
				// React uses internal fiber properties for event system
				if (element._reactInternalFiber || element._reactInternalInstance || element.__reactInternalInstance) {
					try {
						// Trigger React's synthetic event system
						const syntheticInputEvent = new InputEvent('input', {
							bubbles: true,
							cancelable: true,
							data: element.value
						});

						// Force React to process this as a synthetic event
						Object.defineProperty(syntheticInputEvent, 'isTrusted', { value: true });
						element.dispatchEvent(syntheticInputEvent);
					} catch (e) {
						console.warn('React synthetic event failed:', e);
					}
				}

				// Special Vue reactivity trigger
				// Vue uses __vueParentComponent or __vue__ for component access
				if (element.__vue__ || element._vnode || element.__vueParentComponent) {
					try {
						// Vue often needs explicit input event with proper timing
						const vueEvent = new Event('input', { bubbles: true });
						setTimeout(() => element.dispatchEvent(vueEvent), 0);
					} catch (e) {
						console.warn('Vue reactivity trigger failed:', e);
					}
				}

				return success;
			}
			"""

			# Execute the framework events script
			result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': object_id,
					'functionDeclaration': framework_events_script,
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			)

			success = result.get('result', {}).get('value', False)
			if success:
				self.logger.debug('✅ Framework events triggered successfully')
			else:
				self.logger.warning('⚠️ Failed to trigger framework events')

		except Exception as e:
			self.logger.warning(f'⚠️ Failed to trigger framework events: {type(e).__name__}: {e}')
