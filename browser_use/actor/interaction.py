"""Low-level element and coordinate interaction mechanics owned by the actor layer."""

import asyncio

from browser_use.browser.views import BrowserError, URLNotAllowedError


class ElementInteractor:
	"""Execute CDP element and coordinate clicks without event orchestration."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def is_element_occluded(self, backend_node_id: int, x: float, y: float, cdp_session) -> bool:
		"""Check if an element is occluded by other elements at the given coordinates.

		Args:
			backend_node_id: The backend node ID of the target element
			x: X coordinate to check
			y: Y coordinate to check
			cdp_session: CDP session to use

		Returns:
			True if element is occluded, False if clickable
		"""
		try:
			session_id = cdp_session.session_id

			# Get target element info for comparison
			target_result = await cdp_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)

			if 'object' not in target_result:
				self.logger.debug('Could not resolve target element, assuming occluded')
				return True

			object_id = target_result['object']['objectId']

			# Get target element info
			target_info_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': object_id,
					'functionDeclaration': """
					function() {
						const getElementInfo = (el) => {
							return {
								tagName: el.tagName,
								id: el.id || '',
								className: el.className || '',
								textContent: (el.textContent || '').substring(0, 100)
							};
						};


						const elementAtPoint = document.elementFromPoint(arguments[0], arguments[1]);
						if (!elementAtPoint) {
							return { targetInfo: getElementInfo(this), isClickable: false };
						}


						// Simple containment-based clickability logic
						let isClickable = this === elementAtPoint ||
							this.contains(elementAtPoint) ||
							elementAtPoint.contains(this);

						// Check label-input associations when containment check fails
						if (!isClickable) {
							const target = this;
							const atPoint = elementAtPoint;

							// Case 1: target is <input>, atPoint is its associated <label> (or child of that label)
							if (target.tagName === 'INPUT' && target.id) {
								const escapedId = CSS.escape(target.id);
								const assocLabel = document.querySelector('label[for="' + escapedId + '"]');
								if (assocLabel && (assocLabel === atPoint || assocLabel.contains(atPoint))) {
									isClickable = true;
								}
							}

							// Case 2: target is <input>, atPoint is inside a <label> ancestor that wraps the target
							if (!isClickable && target.tagName === 'INPUT') {
								let ancestor = atPoint;
								for (let i = 0; i < 3 && ancestor; i++) {
									if (ancestor.tagName === 'LABEL' && ancestor.contains(target)) {
										isClickable = true;
										break;
									}
									ancestor = ancestor.parentElement;
								}
							}

							// Case 3: target is <label>, atPoint is the associated <input>
							if (!isClickable && target.tagName === 'LABEL') {
								if (target.htmlFor && atPoint.tagName === 'INPUT' && atPoint.id === target.htmlFor) {
									isClickable = true;
								}
								// Also check if atPoint is an input inside the label
								if (!isClickable && atPoint.tagName === 'INPUT' && target.contains(atPoint)) {
									isClickable = true;
								}
							}
						}

						return {
							targetInfo: getElementInfo(this),
							elementAtPointInfo: getElementInfo(elementAtPoint),
							isClickable: isClickable
						};
					}
					""",
					'arguments': [{'value': x}, {'value': y}],
					'returnByValue': True,
				},
				session_id=session_id,
			)

			if 'result' not in target_info_result or 'value' not in target_info_result['result']:
				self.logger.debug('Could not get target element info, assuming occluded')
				return True

			target_data = target_info_result['result']['value']
			is_clickable = target_data.get('isClickable', False)

			if is_clickable:
				self.logger.debug('Element is clickable (target, contained, or semantically related)')
				return False
			else:
				target_info = target_data.get('targetInfo', {})
				element_at_point_info = target_data.get('elementAtPointInfo', {})
				self.logger.debug(
					f'Element is occluded. Target: {target_info.get("tagName", "unknown")} '
					f'(id={target_info.get("id", "none")}), '
					f'ElementAtPoint: {element_at_point_info.get("tagName", "unknown")} '
					f'(id={element_at_point_info.get("id", "none")})'
				)
				return True

		except Exception as e:
			self.logger.debug(f'Occlusion check failed: {e}, assuming not occluded')
			return False

	async def click_element(self, element_node) -> dict | None:
		"""
		Click an element using pure CDP with multiple fallback methods for getting element geometry.

		Args:
			element_node: The DOM element to click
		"""

		try:
			# Check if element is a file input or select dropdown - these should not be clicked
			tag_name = element_node.tag_name.lower() if element_node.tag_name else ''
			element_type = element_node.attributes.get('type', '').lower() if element_node.attributes else ''
			selector_index = self.browser_session.dom_state.get_selector_index(element_node)

			if tag_name == 'select':
				msg = f'Cannot click on <select> elements. Use dropdown_options(index={selector_index}) action instead.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			if tag_name == 'input' and element_type == 'file':
				msg = f'Cannot click on file input element (index={selector_index}). File uploads must be handled using upload_file_to_element action.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			# Get CDP client
			cdp_session = await self.browser_session.session_manager.frames.cdp_client_for_node(element_node)

			# Get the correct session ID for the element's frame
			session_id = cdp_session.session_id

			# Get element bounds
			backend_node_id = element_node.backend_node_id

			# For checkbox/radio: capture pre-click state to verify toggle worked
			is_toggle_element = tag_name == 'input' and element_type in ('checkbox', 'radio')
			pre_click_checked: bool | None = None
			checkbox_object_id: str | None = None
			if is_toggle_element and backend_node_id:
				try:
					resolve_res = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id}, session_id=session_id
					)
					obj_info = resolve_res.get('object', {})
					checkbox_object_id = obj_info.get('objectId') if obj_info else None
					if not checkbox_object_id:
						raise Exception('Failed to resolve checkbox element objectId')
					state_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { return this.checked; }',
							'objectId': checkbox_object_id,
							'returnByValue': True,
						},
						session_id=session_id,
					)
					pre_click_checked = state_res.get('result', {}).get('value')
					self.logger.debug(f'Checkbox pre-click state: checked={pre_click_checked}')
				except Exception as e:
					self.logger.debug(f'Could not capture pre-click checkbox state: {e}')

			# Get viewport dimensions for visibility checks
			layout_metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=session_id)
			viewport_width = layout_metrics['layoutViewport']['clientWidth']
			viewport_height = layout_metrics['layoutViewport']['clientHeight']

			# Scroll element into view FIRST before getting coordinates
			try:
				await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
					params={'backendNodeId': backend_node_id}, session_id=session_id
				)
				await asyncio.sleep(0.05)  # Wait for scroll to complete
				self.logger.debug('Scrolled element into view before getting coordinates')
			except Exception as e:
				self.logger.debug(f'Failed to scroll element into view: {e}')

			# Get element coordinates using the unified method AFTER scrolling
			element_rect = await self.browser_session.dom_state.get_element_coordinates(backend_node_id, cdp_session)

			# Convert rect to quads format if we got coordinates
			quads = []
			if element_rect:
				# Convert DOMRect to quad format
				x, y, w, h = element_rect.x, element_rect.y, element_rect.width, element_rect.height
				quads = [
					[
						x,
						y,  # top-left
						x + w,
						y,  # top-right
						x + w,
						y + h,  # bottom-right
						x,
						y + h,  # bottom-left
					]
				]
				self.logger.debug(
					f'Got coordinates from unified method: {element_rect.x}, {element_rect.y}, {element_rect.width}x{element_rect.height}'
				)

			# If we still don't have quads, fall back to JS click
			if not quads:
				self.logger.warning('Could not get element geometry from any method, falling back to JavaScript click')
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.05)
					# Navigation is handled by BrowserSession via events
					return None
				except Exception as js_e:
					self.logger.warning(f'CDP JavaScript click also failed: {js_e}')
					if 'No node with given id found' in str(js_e):
						raise Exception('Element with given id not found')
					else:
						raise Exception(f'Failed to click element: {js_e}')

			# Find the largest visible quad within the viewport
			best_quad = None
			best_area = 0

			for quad in quads:
				if len(quad) < 8:
					continue

				# Calculate quad bounds
				xs = [quad[i] for i in range(0, 8, 2)]
				ys = [quad[i] for i in range(1, 8, 2)]
				min_x, max_x = min(xs), max(xs)
				min_y, max_y = min(ys), max(ys)

				# Check if quad intersects with viewport
				if max_x < 0 or max_y < 0 or min_x > viewport_width or min_y > viewport_height:
					continue  # Quad is completely outside viewport

				# Calculate visible area (intersection with viewport)
				visible_min_x = max(0, min_x)
				visible_max_x = min(viewport_width, max_x)
				visible_min_y = max(0, min_y)
				visible_max_y = min(viewport_height, max_y)

				visible_width = visible_max_x - visible_min_x
				visible_height = visible_max_y - visible_min_y
				visible_area = visible_width * visible_height

				if visible_area > best_area:
					best_area = visible_area
					best_quad = quad

			if not best_quad:
				# No visible quad found, use the first quad anyway
				best_quad = quads[0]
				self.logger.warning('No visible quad found, using first quad')

			# Calculate center point of the best quad
			center_x = sum(best_quad[i] for i in range(0, 8, 2)) / 4
			center_y = sum(best_quad[i] for i in range(1, 8, 2)) / 4

			# Ensure click point is within viewport bounds
			center_x = max(0, min(viewport_width - 1, center_x))
			center_y = max(0, min(viewport_height - 1, center_y))

			# Check for occlusion before attempting CDP click
			is_occluded = await self.is_element_occluded(backend_node_id, center_x, center_y, cdp_session)

			if is_occluded:
				self.logger.debug('🚫 Element is occluded, falling back to JavaScript click')
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.05)
					return None
				except Exception as js_e:
					self.logger.error(f'JavaScript click fallback failed: {js_e}')
					raise Exception(f'Failed to click occluded element: {js_e}')

			# Perform the click using CDP (element is not occluded)
			try:
				self.logger.debug(f'👆 Dragging mouse over element before clicking x: {center_x}px y: {center_y}px ...')
				# Move mouse to element
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseMoved',
						'x': center_x,
						'y': center_y,
					},
					session_id=session_id,
				)
				await asyncio.sleep(0.05)

				# Mouse down
				self.logger.debug(f'👆🏾 Clicking x: {center_x}px y: {center_y}px ...')
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mousePressed',
								'x': center_x,
								'y': center_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=3.0,  # 3 second timeout for mousePressed
					)
					await asyncio.sleep(0.08)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')
					# Don't sleep if we timed out

				# Mouse up
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mouseReleased',
								'x': center_x,
								'y': center_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=5.0,  # 5 second timeout for mouseReleased
					)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

				self.logger.debug('🖱️ Clicked successfully using x,y coordinates')

				# For checkbox/radio: verify state toggled, fall back to JS element.click() if not
				if is_toggle_element and pre_click_checked is not None and checkbox_object_id:
					try:
						await asyncio.sleep(0.05)
						state_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
							params={
								'functionDeclaration': 'function() { return this.checked; }',
								'objectId': checkbox_object_id,
								'returnByValue': True,
							},
							session_id=session_id,
						)
						post_click_checked = state_res.get('result', {}).get('value')
						if post_click_checked == pre_click_checked:
							# CDP mouse events didn't toggle the checkbox — try JS element.click()
							self.logger.debug(
								f'Checkbox state unchanged after CDP click (checked={pre_click_checked}), using JS fallback'
							)
							await cdp_session.cdp_client.send.Runtime.callFunctionOn(
								params={'functionDeclaration': 'function() { this.click(); }', 'objectId': checkbox_object_id},
								session_id=session_id,
							)
							await asyncio.sleep(0.05)
							final_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
								params={
									'functionDeclaration': 'function() { return this.checked; }',
									'objectId': checkbox_object_id,
									'returnByValue': True,
								},
								session_id=session_id,
							)
							post_click_checked = final_res.get('result', {}).get('value')
						self.logger.debug(f'Checkbox post-click state: checked={post_click_checked}')
						return {'click_x': center_x, 'click_y': center_y, 'checked': post_click_checked}
					except Exception as e:
						self.logger.debug(f'Checkbox state verification failed (non-critical): {e}')

				# Return coordinates as dict for metadata
				return {'click_x': center_x, 'click_y': center_y}

			except Exception as e:
				self.logger.warning(f'CDP click failed: {type(e).__name__}: {e}')
				# Fall back to JavaScript click via CDP
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)

					# Small delay for dialog dismissal
					await asyncio.sleep(0.1)

					return None
				except Exception as js_e:
					self.logger.warning(f'CDP JavaScript click also failed: {js_e}')
					raise Exception(f'Failed to click element: {e}')
			finally:
				# Always re-focus back to original top-level page session context in case click opened a new tab/popup/window/dialog/etc.
				# Use timeout to prevent hanging if dialog is blocking
				try:
					cdp_session = await asyncio.wait_for(self.browser_session.get_or_create_cdp_session(focus=True), timeout=3.0)
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Runtime.runIfWaitingForDebugger(session_id=cdp_session.session_id),
						timeout=2.0,
					)
				except TimeoutError:
					self.logger.debug('⏱️ Refocus after click timed out (page may be blocked by dialog). Continuing...')
				except Exception as e:
					self.logger.debug(f'⚠️ Refocus error (non-critical): {type(e).__name__}: {e}')

		except URLNotAllowedError as e:
			raise e
		except BrowserError as e:
			raise e
		except Exception as e:
			# Extract key element info for error message
			selector_index = self.browser_session.dom_state.get_selector_index(element_node)
			element_info = f'<{element_node.tag_name or "unknown"}'
			if selector_index:
				element_info += f' index={selector_index}'
			element_info += '>'

			# Create helpful error message based on context
			error_detail = f'Failed to click element {element_info}. The element may not be interactable or visible.'

			# Add hint if element has index (common in code-use mode)
			if selector_index:
				error_detail += f' If the page changed after navigation/interaction, the index [{selector_index}] may be stale. Get fresh browser state before retrying.'

			raise BrowserError(
				message=f'Failed to click element: {str(e)}',
				long_term_memory=error_detail,
			)

	async def click_coordinate(self, coordinate_x: int, coordinate_y: int, force: bool = False) -> dict | None:
		"""
		Click directly at coordinates using CDP Input.dispatchMouseEvent.

		Args:
			coordinate_x: X coordinate in viewport
			coordinate_y: Y coordinate in viewport
			force: If True, skip all safety checks (used when force=True in event)

		Returns:
			Dict with click coordinates or None
		"""
		try:
			# Get CDP session
			cdp_session = await self.browser_session.get_or_create_cdp_session()
			session_id = cdp_session.session_id

			self.logger.debug(f'👆 Moving mouse to ({coordinate_x}, {coordinate_y})...')

			# Move mouse to coordinates
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseMoved',
					'x': coordinate_x,
					'y': coordinate_y,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Mouse down
			self.logger.debug(f'👆🏾 Clicking at ({coordinate_x}, {coordinate_y})...')
			try:
				await asyncio.wait_for(
					cdp_session.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mousePressed',
							'x': coordinate_x,
							'y': coordinate_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=session_id,
					),
					timeout=3.0,
				)
				await asyncio.sleep(0.05)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')

			# Mouse up
			try:
				await asyncio.wait_for(
					cdp_session.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mouseReleased',
							'x': coordinate_x,
							'y': coordinate_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=session_id,
					),
					timeout=5.0,
				)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

			self.logger.debug(f'🖱️ Clicked successfully at ({coordinate_x}, {coordinate_y})')

			# Return coordinates as metadata
			return {'click_x': coordinate_x, 'click_y': coordinate_y}

		except Exception as e:
			self.logger.error(f'Failed to click at coordinates ({coordinate_x}, {coordinate_y}): {type(e).__name__}: {e}')
			raise BrowserError(
				message=f'Failed to click at coordinates: {e}',
				long_term_memory=f'Failed to click at coordinates ({coordinate_x}, {coordinate_y}). The coordinates may be outside viewport or the page may have changed.',
			)
