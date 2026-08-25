"""Browser-session DOM cache, lookup, and highlighting behavior."""

import asyncio
from typing import TYPE_CHECKING, Any

from browser_use.browser.session_manager import CDPSession
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserDomState:
	"""Own DOM-derived state and element operations for one browser session."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session
		self._cached_selector_map: dict[int, EnhancedDOMTreeNode] = {}
		self._cached_selector_indices: dict[tuple[str, int], int] = {}
		self.cached_browser_state_summary: BrowserStateSummary | None = None
		self.original_viewport_size: tuple[int, int] | None = None

	def clear(self) -> None:
		self._cached_selector_map.clear()
		self._cached_selector_indices.clear()
		self.cached_browser_state_summary = None
		self.original_viewport_size = None

	async def get_dom_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element by index.

		Get element from cached selector map.

		Args:
			index: The element index from the serialized DOM

		Returns:
			EnhancedDOMTreeNode or None if index not found
		"""
		#  Check cached selector map
		if self._cached_selector_map and index in self._cached_selector_map:
			return self._cached_selector_map[index]

		return None

	def get_selector_index(self, node: EnhancedDOMTreeNode) -> int:
		"""Return the model-visible selector index for a DOM node."""
		node_identity = (str(node.session_id), node.backend_node_id)
		return self._cached_selector_indices.get(node_identity, node.backend_node_id)

	def _get_cached_node_by_backend_id(self, backend_node_id: int, session_id: str | None) -> EnhancedDOMTreeNode | None:
		"""Resolve a backend ID only within the CDP session that produced it."""
		for node in (self._cached_selector_map or {}).values():
			if node.backend_node_id == backend_node_id and str(node.session_id) == str(session_id):
				return node
		return None

	def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		"""Update the cached selector map with new DOM state.

		This should be called by the DOM watchdog after rebuilding the DOM.

		Args:
			selector_map: The new selector map from DOM serialization
		"""
		self._cached_selector_map = selector_map
		self._cached_selector_indices = {
			(str(node.session_id), node.backend_node_id): index for index, node in selector_map.items()
		}

	async def get_dom_element_at_coordinates(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
		"""Get DOM element at coordinates as EnhancedDOMTreeNode.

		First checks the cached selector_map for a matching element, then falls back
		to CDP DOM.describeNode if not found. This ensures safety checks (e.g., for
		<select> elements and file inputs) work correctly.

		Args:
			x: X coordinate relative to viewport
			y: Y coordinate relative to viewport

		Returns:
			EnhancedDOMTreeNode at the coordinates, or None if no element found
		"""
		from browser_use.dom.views import NodeType

		# Get current page to access CDP session
		page = await self.browser_session.get_current_page()
		if page is None:
			raise RuntimeError('No active page found')

		# Get session ID for CDP call
		session_id = await page._ensure_session()

		try:
			# Call CDP DOM.getNodeForLocation to get backend_node_id
			result = await self.browser_session.cdp_client.send.DOM.getNodeForLocation(
				params={
					'x': x,
					'y': y,
					'includeUserAgentShadowDOM': False,
					'ignorePointerEventsNone': False,
				},
				session_id=session_id,
			)

			backend_node_id = result.get('backendNodeId')
			if backend_node_id is None:
				self.browser_session.logger.debug(f'No element found at coordinates ({x}, {y})')
				return None

			# Try to find element in cached selector_map (avoids extra CDP call)
			cached_node = self._get_cached_node_by_backend_id(backend_node_id, session_id)
			if cached_node is not None:
				self.browser_session.logger.debug(f'Found element at ({x}, {y}) in cached selector_map')
				return cached_node

			# Not in cache - fall back to CDP DOM.describeNode to get actual node info
			try:
				describe_result = await self.browser_session.cdp_client.send.DOM.describeNode(
					params={'backendNodeId': backend_node_id},
					session_id=session_id,
				)
				node_info = describe_result.get('node', {})
				node_name = node_info.get('nodeName', '')

				# Parse attributes from flat list [key1, val1, key2, val2, ...] to dict
				attrs_list = node_info.get('attributes', [])
				attributes = {attrs_list[i]: attrs_list[i + 1] for i in range(0, len(attrs_list), 2)}

				return EnhancedDOMTreeNode(
					node_id=result.get('nodeId', 0),
					backend_node_id=backend_node_id,
					node_type=NodeType(node_info.get('nodeType', NodeType.ELEMENT_NODE.value)),
					node_name=node_name,
					node_value=node_info.get('nodeValue', '') or '',
					attributes=attributes,
					is_scrollable=None,
					frame_id=result.get('frameId'),
					session_id=session_id,
					target_id=self.browser_session.agent_focus_target_id or '',
					content_document=None,
					shadow_root_type=None,
					shadow_roots=None,
					parent_node=None,
					children_nodes=None,
					ax_node=None,
					snapshot_node=None,
					is_visible=None,
					absolute_position=None,
				)
			except Exception as e:
				self.browser_session.logger.debug(f'DOM.describeNode failed for backend_node_id={backend_node_id}: {e}')
				# Fall back to minimal node if describeNode fails
				return EnhancedDOMTreeNode(
					node_id=result.get('nodeId', 0),
					backend_node_id=backend_node_id,
					node_type=NodeType.ELEMENT_NODE,
					node_name='',
					node_value='',
					attributes={},
					is_scrollable=None,
					frame_id=result.get('frameId'),
					session_id=session_id,
					target_id=self.browser_session.agent_focus_target_id or '',
					content_document=None,
					shadow_root_type=None,
					shadow_roots=None,
					parent_node=None,
					children_nodes=None,
					ax_node=None,
					snapshot_node=None,
					is_visible=None,
					absolute_position=None,
				)

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to get DOM element at coordinates ({x}, {y}): {e}')
			return None

	def is_file_input(self, element: Any) -> bool:
		"""Check if element is a file input.

		Args:
			element: The DOM element to check

		Returns:
			True if element is a file input, False otherwise
		"""
		if self.browser_session.watchdogs.dom:
			return self.browser_session.watchdogs.dom.is_file_input(element)
		# Fallback if watchdog not available
		return (
			hasattr(element, 'node_name')
			and element.node_name.upper() == 'INPUT'
			and hasattr(element, 'attributes')
			and element.attributes.get('type', '').lower() == 'file'
		)

	def find_file_input_near_element(
		self,
		node: 'EnhancedDOMTreeNode',
		max_height: int = 3,
		max_descendant_depth: int = 3,
	) -> 'EnhancedDOMTreeNode | None':
		"""Find the closest file input to the given element.

		Walks up the DOM tree (up to max_height levels), checking the node itself,
		its descendants (up to max_descendant_depth deep), and siblings at each level.

		Args:
			node: Starting DOM element
			max_height: Maximum levels to walk up the parent chain
			max_descendant_depth: Maximum depth to search descendants

		Returns:
			The nearest file input element, or None if not found
		"""
		from browser_use.dom.views import EnhancedDOMTreeNode

		def _find_in_descendants(n: EnhancedDOMTreeNode, depth: int) -> EnhancedDOMTreeNode | None:
			if depth < 0:
				return None
			if self.is_file_input(n):
				return n
			for child in n.children_nodes or []:
				result = _find_in_descendants(child, depth - 1)
				if result:
					return result
			return None

		current: EnhancedDOMTreeNode | None = node
		for _ in range(max_height + 1):
			if current is None:
				break
			# Check the current node itself
			if self.is_file_input(current):
				return current
			# Check all descendants of the current node
			result = _find_in_descendants(current, max_descendant_depth)
			if result:
				return result
			# Check all siblings and their descendants
			if current.parent_node:
				for sibling in current.parent_node.children_nodes or []:
					if sibling is current:
						continue
					if self.is_file_input(sibling):
						return sibling
					result = _find_in_descendants(sibling, max_descendant_depth)
					if result:
						return result
			current = current.parent_node
		return None

	async def get_selector_map(self) -> dict[int, EnhancedDOMTreeNode]:
		"""Get the current selector map from cached state or DOM watchdog.

		Returns:
			Dictionary mapping element indices to EnhancedDOMTreeNode objects
		"""
		# First try cached selector map
		if self._cached_selector_map:
			return self._cached_selector_map

		# Try to get from DOM watchdog
		if self.browser_session.watchdogs.dom and hasattr(self.browser_session.watchdogs.dom, 'selector_map'):
			return self.browser_session.watchdogs.dom.selector_map or {}

		# Return empty dict if nothing available
		return {}

	async def get_index_by_id(self, element_id: str) -> int | None:
		"""Find element index by its id attribute.

		Args:
			element_id: The id attribute value to search for

		Returns:
			Index of the element, or None if not found
		"""
		selector_map = await self.get_selector_map()
		for idx, element in selector_map.items():
			if element.attributes and element.attributes.get('id') == element_id:
				return idx
		return None

	async def remove_highlights(self) -> None:
		"""Remove highlights from the page using CDP."""
		if (
			not self.browser_session.browser_profile.highlight_elements
			and not self.browser_session.browser_profile.dom_highlight_elements
		):
			return

		try:
			async with asyncio.timeout(3.0):
				# Get cached session
				cdp_session = await self.browser_session.get_or_create_cdp_session()

				# Remove highlights via JavaScript - be thorough
				script = """
				(function() {
					// Remove all browser-use highlight elements
					const highlights = document.querySelectorAll('[data-browser-use-highlight]');
					console.log('Removing', highlights.length, 'browser-use highlight elements');
					highlights.forEach(el => el.remove());

					// Also remove by ID in case selector missed anything
					const highlightContainer = document.getElementById('browser-use-debug-highlights');
					if (highlightContainer) {
						console.log('Removing highlight container by ID');
						highlightContainer.remove();
					}

					// Final cleanup - remove any orphaned tooltips
					const orphanedTooltips = document.querySelectorAll('[data-browser-use-highlight="tooltip"]');
					orphanedTooltips.forEach(el => el.remove());

					return { removed: highlights.length };
				})();
				"""
				result = await cdp_session.cdp_client.send.Runtime.evaluate(
					params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
				)

				# Log the result for debugging
				if result and 'result' in result and 'value' in result['result']:
					removed_count = result['result']['value'].get('removed', 0)
					self.browser_session.logger.debug(f'Successfully removed {removed_count} highlight elements')
				else:
					self.browser_session.logger.debug('Highlight removal completed')

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to remove highlights: {e}')

	async def get_element_coordinates(self, backend_node_id: int, cdp_session: CDPSession) -> DOMRect | None:
		"""Get element coordinates for a backend node ID using multiple methods.

		This method tries DOM.getContentQuads first, then falls back to DOM.getBoxModel,
		and finally uses JavaScript getBoundingClientRect as a last resort.

		Args:
			backend_node_id: The backend node ID to get coordinates for
			cdp_session: The CDP session to use

		Returns:
			DOMRect with coordinates or None if element not found/no bounds
		"""
		session_id = cdp_session.session_id
		quads = []

		# Method 1: Try DOM.getContentQuads first (best for inline elements and complex layouts)
		try:
			content_quads_result = await cdp_session.cdp_client.send.DOM.getContentQuads(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)
			if 'quads' in content_quads_result and content_quads_result['quads']:
				quads = content_quads_result['quads']
				self.browser_session.logger.debug(f'Got {len(quads)} quads from DOM.getContentQuads')
			else:
				self.browser_session.logger.debug(f'No quads found from DOM.getContentQuads {content_quads_result}')
		except Exception as e:
			self.browser_session.logger.debug(f'DOM.getContentQuads failed: {e}')

		# Method 2: Fall back to DOM.getBoxModel
		if not quads:
			try:
				box_model = await cdp_session.cdp_client.send.DOM.getBoxModel(
					params={'backendNodeId': backend_node_id}, session_id=session_id
				)
				if 'model' in box_model and 'content' in box_model['model']:
					content_quad = box_model['model']['content']
					if len(content_quad) >= 8:
						# Convert box model format to quad format
						quads = [
							[
								content_quad[0],
								content_quad[1],  # x1, y1
								content_quad[2],
								content_quad[3],  # x2, y2
								content_quad[4],
								content_quad[5],  # x3, y3
								content_quad[6],
								content_quad[7],  # x4, y4
							]
						]
						self.browser_session.logger.debug('Got quad from DOM.getBoxModel')
			except Exception as e:
				self.browser_session.logger.debug(f'DOM.getBoxModel failed: {e}')

		# Method 3: Fall back to JavaScript getBoundingClientRect
		if not quads:
			try:
				result = await cdp_session.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': backend_node_id},
					session_id=session_id,
				)
				if 'object' in result and 'objectId' in result['object']:
					object_id = result['object']['objectId']
					js_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'objectId': object_id,
							'functionDeclaration': """
							function() {
								const rect = this.getBoundingClientRect();
								return {
									x: rect.x,
									y: rect.y,
									width: rect.width,
									height: rect.height
								};
							}
							""",
							'returnByValue': True,
						},
						session_id=session_id,
					)
					if 'result' in js_result and 'value' in js_result['result']:
						rect_data = js_result['result']['value']
						if rect_data['width'] > 0 and rect_data['height'] > 0:
							return DOMRect(
								x=rect_data['x'], y=rect_data['y'], width=rect_data['width'], height=rect_data['height']
							)
			except Exception as e:
				self.browser_session.logger.debug(f'JavaScript getBoundingClientRect failed: {e}')

		# Convert quads to bounding rectangle if we have them
		if quads:
			# Use the first quad (most relevant for the element)
			quad = quads[0]
			if len(quad) >= 8:
				# Calculate bounding rect from quad points
				x_coords = [quad[i] for i in range(0, 8, 2)]
				y_coords = [quad[i] for i in range(1, 8, 2)]

				min_x = min(x_coords)
				min_y = min(y_coords)
				max_x = max(x_coords)
				max_y = max(y_coords)

				width = max_x - min_x
				height = max_y - min_y

				if width > 0 and height > 0:
					return DOMRect(x=min_x, y=min_y, width=width, height=height)

		return None

	async def highlight_interaction_element(self, node: 'EnhancedDOMTreeNode') -> None:
		"""Temporarily highlight an element during interaction for user visibility.

		This creates a visual highlight on the browser that shows the user which element
		is being interacted with. The highlight automatically fades after the configured duration.

		Args:
			node: The DOM node to highlight with backend_node_id for coordinate lookup
		"""
		if not self.browser_session.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.browser_session.session_manager.cdp_client_for_node(node)

			# Get current coordinates
			rect = await self.get_element_coordinates(node.backend_node_id, cdp_session)

			color = self.browser_session.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_session.browser_profile.interaction_highlight_duration * 1000)

			if not rect:
				self.browser_session.logger.debug(f'No coordinates found for backend node {node.backend_node_id}')
				return

			# Create animated corner brackets that start offset and animate inward
			script = f"""
			(function() {{
				const rect = {json.dumps({'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height})};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Scale corner size based on element dimensions to ensure gaps between corners
				const maxCornerSize = 20;
				const minCornerSize = 8;
				const cornerSize = Math.max(
					minCornerSize,
					Math.min(maxCornerSize, Math.min(rect.width, rect.height) * 0.35)
				);
				const borderWidth = 3;
				const startOffset = 10; // Starting offset in pixels
				const finalOffset = -3; // Final position slightly outside the element

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container for all corners
				const container = document.createElement('div');
				container.setAttribute('data-browser-use-interaction-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{rect.x + scrollX}}px;
					top: ${{rect.y + scrollY}}px;
					width: ${{rect.width}}px;
					height: ${{rect.height}}px;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create 4 corner brackets
				const corners = [
					{{ pos: 'top-left', startX: -startOffset, startY: -startOffset, finalX: finalOffset, finalY: finalOffset }},
					{{ pos: 'top-right', startX: startOffset, startY: -startOffset, finalX: -finalOffset, finalY: finalOffset }},
					{{ pos: 'bottom-left', startX: -startOffset, startY: startOffset, finalX: finalOffset, finalY: -finalOffset }},
					{{ pos: 'bottom-right', startX: startOffset, startY: startOffset, finalX: -finalOffset, finalY: -finalOffset }}
				];

				corners.forEach(corner => {{
					const bracket = document.createElement('div');
					bracket.style.cssText = `
						position: absolute;
						width: ${{cornerSize}}px;
						height: ${{cornerSize}}px;
						pointer-events: none;
						transition: all 0.15s ease-out;
					`;

					// Position corners
					if (corner.pos === 'top-left') {{
						bracket.style.top = '0';
						bracket.style.left = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'top-right') {{
						bracket.style.top = '0';
						bracket.style.right = '0';
						bracket.style.borderTop = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-left') {{
						bracket.style.bottom = '0';
						bracket.style.left = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderLeft = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}} else if (corner.pos === 'bottom-right') {{
						bracket.style.bottom = '0';
						bracket.style.right = '0';
						bracket.style.borderBottom = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.borderRight = `${{borderWidth}}px solid ${{color}}`;
						bracket.style.transform = `translate(${{corner.startX}}px, ${{corner.startY}}px)`;
					}}

					container.appendChild(bracket);

					// Animate to final position slightly outside the element
					setTimeout(() => {{
						bracket.style.transform = `translate(${{corner.finalX}}px, ${{corner.finalY}}px)`;
					}}, 10);
				}});

				document.body.appendChild(container);

				// Auto-remove after duration
				setTimeout(() => {{
					container.style.opacity = '0';
					container.style.transition = 'opacity 0.3s ease-out';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion

			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.browser_session.logger.debug(f'Failed to highlight interaction element: {e}')

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		"""Temporarily highlight a coordinate click position for user visibility.

		This creates a visual highlight at the specified coordinates showing where
		the click action occurred. The highlight automatically fades after the configured duration.

		Args:
			x: Horizontal coordinate relative to viewport left edge
			y: Vertical coordinate relative to viewport top edge
		"""
		if not self.browser_session.browser_profile.highlight_elements:
			return

		try:
			import json

			cdp_session = await self.browser_session.get_or_create_cdp_session()

			color = self.browser_session.browser_profile.interaction_highlight_color
			duration_ms = int(self.browser_session.browser_profile.interaction_highlight_duration * 1000)

			# Create animated crosshair and circle at the click coordinates
			script = f"""
			(function() {{
				const x = {x};
				const y = {y};
				const color = {json.dumps(color)};
				const duration = {duration_ms};

				// Get current scroll position
				const scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
				const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

				// Create container
				const container = document.createElement('div');
				container.setAttribute('data-browser-use-coordinate-highlight', 'true');
				container.style.cssText = `
					position: absolute;
					left: ${{x + scrollX}}px;
					top: ${{y + scrollY}}px;
					width: 0;
					height: 0;
					pointer-events: none;
					z-index: 2147483647;
				`;

				// Create outer circle
				const outerCircle = document.createElement('div');
				outerCircle.style.cssText = `
					position: absolute;
					left: -15px;
					top: -15px;
					width: 30px;
					height: 30px;
					border: 3px solid ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0.3);
					transition: all 0.2s ease-out;
				`;
				container.appendChild(outerCircle);

				// Create center dot
				const centerDot = document.createElement('div');
				centerDot.style.cssText = `
					position: absolute;
					left: -4px;
					top: -4px;
					width: 8px;
					height: 8px;
					background: ${{color}};
					border-radius: 50%;
					opacity: 0;
					transform: scale(0);
					transition: all 0.15s ease-out;
				`;
				container.appendChild(centerDot);

				document.body.appendChild(container);

				// Animate in
				setTimeout(() => {{
					outerCircle.style.opacity = '0.8';
					outerCircle.style.transform = 'scale(1)';
					centerDot.style.opacity = '1';
					centerDot.style.transform = 'scale(1)';
				}}, 10);

				// Animate out and remove
				setTimeout(() => {{
					outerCircle.style.opacity = '0';
					outerCircle.style.transform = 'scale(1.5)';
					centerDot.style.opacity = '0';
					setTimeout(() => container.remove(), 300);
				}}, duration);

				return {{ created: true }};
			}})();
			"""

			# Fire and forget - don't wait for completion
			await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

		except Exception as e:
			# Don't fail the action if highlighting fails
			self.browser_session.logger.debug(f'Failed to highlight coordinate click: {e}')

	async def add_highlights(self, selector_map: dict[int, 'EnhancedDOMTreeNode']) -> None:
		"""Add visual highlights to the browser DOM for user visibility."""
		if not self.browser_session.browser_profile.dom_highlight_elements or not selector_map:
			return

		try:
			import json

			# Convert selector_map to the format expected by the highlighting script
			elements_data = []
			for element_index, node in selector_map.items():
				# Get bounding box using absolute position (includes iframe translations) if available
				if node.absolute_position:
					# Use absolute position which includes iframe coordinate translations
					rect = node.absolute_position
					bbox = {'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height}

					# Only include elements with valid bounding boxes
					if bbox and bbox.get('width', 0) > 0 and bbox.get('height', 0) > 0:
						element = {
							'x': bbox['x'],
							'y': bbox['y'],
							'width': bbox['width'],
							'height': bbox['height'],
							'element_name': node.node_name,
							'is_clickable': node.snapshot_node.is_clickable if node.snapshot_node else True,
							'is_scrollable': getattr(node, 'is_scrollable', False),
							'attributes': node.attributes or {},
							'frame_id': getattr(node, 'frame_id', None),
							'node_id': node.node_id,
							'backend_node_id': node.backend_node_id,
							'element_index': element_index,
							'xpath': node.xpath,
							'text_content': node.get_all_children_text()[:50]
							if hasattr(node, 'get_all_children_text')
							else node.node_value[:50],
						}
						elements_data.append(element)

			if not elements_data:
				self.browser_session.logger.debug('⚠️ No valid elements to highlight')
				return

			self.browser_session.logger.debug(f'📍 Creating highlights for {len(elements_data)} elements')

			# Always remove existing highlights first
			await self.remove_highlights()

			# Add a small delay to ensure removal completes
			import asyncio

			await asyncio.sleep(0.05)

			# Get CDP session
			cdp_session = await self.browser_session.get_or_create_cdp_session()

			# Create the proven highlighting script from v0.6.0 with fixed positioning
			script = f"""
			(function() {{
				// Interactive elements data
				const interactiveElements = {json.dumps(elements_data)};

				console.log('=== BROWSER-USE HIGHLIGHTING ===');
				console.log('Highlighting', interactiveElements.length, 'interactive elements');

				// Double-check: Remove any existing highlight container first
				const existingContainer = document.getElementById('browser-use-debug-highlights');
				if (existingContainer) {{
					console.log('⚠️ Found existing highlight container, removing it first');
					existingContainer.remove();
				}}

				// Also remove any stray highlight elements
				const strayHighlights = document.querySelectorAll('[data-browser-use-highlight]');
				if (strayHighlights.length > 0) {{
					console.log('⚠️ Found', strayHighlights.length, 'stray highlight elements, removing them');
					strayHighlights.forEach(el => el.remove());
				}}

				// Use maximum z-index for visibility
				const HIGHLIGHT_Z_INDEX = 2147483647;

				// Create container for all highlights - use FIXED positioning (key insight from v0.6.0)
				const container = document.createElement('div');
				container.id = 'browser-use-debug-highlights';
				container.setAttribute('data-browser-use-highlight', 'container');

				container.style.cssText = `
					position: absolute;
					top: 0;
					left: 0;
					width: 100vw;
					height: 100vh;
					pointer-events: none;
					z-index: ${{HIGHLIGHT_Z_INDEX}};
					overflow: visible;
					margin: 0;
					padding: 0;
					border: none;
					outline: none;
					box-shadow: none;
					background: none;
					font-family: inherit;
				`;

				// Helper function to create text elements safely
				function createTextElement(tag, text, styles) {{
					const element = document.createElement(tag);
					element.textContent = text;
					if (styles) element.style.cssText = styles;
					return element;
				}}

				// Add highlights for each element
				interactiveElements.forEach((element, index) => {{
					const highlight = document.createElement('div');
					highlight.setAttribute('data-browser-use-highlight', 'element');
					highlight.setAttribute('data-element-id', element.element_index);
					highlight.style.cssText = `
						position: absolute;
						left: ${{element.x}}px;
						top: ${{element.y}}px;
						width: ${{element.width}}px;
						height: ${{element.height}}px;
						outline: 2px dashed #4a90e2;
						outline-offset: -2px;
						background: transparent;
						pointer-events: none;
						box-sizing: content-box;
						transition: outline 0.2s ease;
						margin: 0;
						padding: 0;
						border: none;
					`;

					// Label with the same selector index shown to the model
					const label = createTextElement('div', element.element_index, `
						position: absolute;
						top: -20px;
						left: 0;
						background-color: #4a90e2;
						color: white;
						padding: 2px 6px;
						font-size: 11px;
						font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
						font-weight: bold;
						border-radius: 3px;
						white-space: nowrap;
						z-index: ${{HIGHLIGHT_Z_INDEX + 1}};
						box-shadow: 0 2px 4px rgba(0,0,0,0.3);
						border: none;
						outline: none;
						margin: 0;
						line-height: 1.2;
					`);

					highlight.appendChild(label);
					container.appendChild(highlight);
				}});

				// Add container to document
				document.body.appendChild(container);

				console.log('Highlighting complete - added', interactiveElements.length, 'highlights');
				return {{ added: interactiveElements.length }};
			}})();
			"""

			# Execute the script
			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': script, 'returnByValue': True}, session_id=cdp_session.session_id
			)

			# Log the result
			if result and 'result' in result and 'value' in result['result']:
				added_count = result['result']['value'].get('added', 0)
				self.browser_session.logger.debug(f'Successfully added {added_count} highlight elements to browser DOM')
			else:
				self.browser_session.logger.debug('Browser highlight injection completed')

		except Exception as e:
			self.browser_session.logger.warning(f'Failed to add browser highlights: {e}')
			import traceback

			self.browser_session.logger.debug(f'Browser highlight traceback: {traceback.format_exc()}')
