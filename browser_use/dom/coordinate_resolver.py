from __future__ import annotations

from typing import TYPE_CHECKING

from browser_use.browser.session_manager import CDPSession
from browser_use.dom.selector_cache import DOMSelectorCache
from browser_use.dom.tree import DOMRect, EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class DOMCoordinateResolver:
	"""Resolve DOM nodes and geometry across CDP sessions and frames."""

	def __init__(self, browser_session: BrowserSession, selectors: DOMSelectorCache) -> None:
		self.browser_session = browser_session
		self.selectors = selectors

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
		from browser_use.dom.tree import NodeType

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
			cached_node = self.selectors.get_by_backend_id(backend_node_id, session_id)
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
