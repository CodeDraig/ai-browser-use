import logging
import time
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.accessibility.types import AXNode
from cdp_use.cdp.target import TargetID

from browser_use.dom.collection import DOMTreeCollector
from browser_use.dom.enhanced_snapshot import build_snapshot_lookup
from browser_use.dom.serialized_state import SerializedDOMState
from browser_use.dom.serializer.clickable_elements import ClickableElementDetector
from browser_use.dom.serializer.serializer import DOMTreeSerializer
from browser_use.dom.tree import (
	DOMRect,
	EnhancedDOMTreeNode,
	NodeType,
)
from browser_use.dom.tree_builder import EnhancedDOMTreeBuilder

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

# Note: iframe limits are now configurable via BrowserProfile.max_iframes and BrowserProfile.max_iframe_depth


class DomService:
	"""
	Service for getting the DOM tree and other DOM-related information.

	Either browser or page must be provided.

	TODO: currently we start a new websocket connection PER STEP, we should definitely keep this persistent
	"""

	logger: logging.Logger

	def __init__(
		self,
		browser_session: 'BrowserSession',
		logger: logging.Logger | None = None,
		cross_origin_iframes: bool = False,
		paint_order_filtering: bool = True,
		max_iframes: int = 100,
		max_iframe_depth: int = 5,
		viewport_threshold: int | None = 1000,
	):
		self.browser_session = browser_session
		self.logger = logger or browser_session.logger
		self.cross_origin_iframes = cross_origin_iframes
		self.paint_order_filtering = paint_order_filtering
		self.max_iframes = max_iframes
		self.max_iframe_depth = max_iframe_depth
		self.viewport_threshold = viewport_threshold
		self.collector = DOMTreeCollector(browser_session, self.logger, max_iframes)

	async def __aenter__(self):
		return self

	async def __aexit__(self, exc_type, exc_value, traceback):
		pass  # no need to cleanup anything, browser_session auto handles cleaning up session cache

	def _count_hidden_elements_in_iframes(self, node: EnhancedDOMTreeNode) -> None:
		"""Collect hidden interactive elements in iframes for LLM hints.

		For each iframe, collects details of hidden interactive elements including
		tag, text/name, and scroll distance in pages so the agent knows how far to scroll.
		"""

		def is_hidden_by_threshold(element: EnhancedDOMTreeNode) -> bool:
			"""Check if element is hidden by viewport threshold (not CSS)."""
			if element.is_visible or not element.snapshot_node or not element.snapshot_node.bounds:
				return False

			computed_styles = element.snapshot_node.computed_styles or {}
			display = computed_styles.get('display', '').lower()
			visibility = computed_styles.get('visibility', '').lower()
			opacity = computed_styles.get('opacity', '1')

			css_hidden = display == 'none' or visibility == 'hidden'
			try:
				css_hidden = css_hidden or float(opacity) <= 0
			except (ValueError, TypeError):
				pass

			return not css_hidden

		def collect_hidden_elements(subtree_root: EnhancedDOMTreeNode, viewport_height: float) -> list[dict[str, Any]]:
			"""Collect hidden interactive elements from subtree."""
			hidden: list[dict[str, Any]] = []

			if subtree_root.node_type == NodeType.ELEMENT_NODE:
				is_interactive = ClickableElementDetector.is_interactive(subtree_root)

				if is_interactive and is_hidden_by_threshold(subtree_root):
					# Get element text/name
					text = ''
					if subtree_root.ax_node and subtree_root.ax_node.name:
						text = subtree_root.ax_node.name[:40]
					elif subtree_root.attributes:
						text = (
							subtree_root.attributes.get('placeholder', '')
							or subtree_root.attributes.get('title', '')
							or subtree_root.attributes.get('aria-label', '')
						)[:40]

					# Get y position and convert to pages
					y_pos = 0.0
					if subtree_root.snapshot_node and subtree_root.snapshot_node.bounds:
						y_pos = subtree_root.snapshot_node.bounds.y
					pages_down = round(y_pos / viewport_height, 1) if viewport_height > 0 else 0

					hidden.append(
						{
							'tag': subtree_root.tag_name or '?',
							'text': text or '(no label)',
							'pages': pages_down,
						}
					)

			for child in subtree_root.children_nodes or []:
				hidden.extend(collect_hidden_elements(child, viewport_height))

			for shadow_root in subtree_root.shadow_roots or []:
				hidden.extend(collect_hidden_elements(shadow_root, viewport_height))

			return hidden

		def has_any_hidden_content(subtree_root: EnhancedDOMTreeNode) -> bool:
			"""Check if there's any hidden content (interactive or not) in subtree."""
			if is_hidden_by_threshold(subtree_root):
				return True

			for child in subtree_root.children_nodes or []:
				if has_any_hidden_content(child):
					return True

			for shadow_root in subtree_root.shadow_roots or []:
				if has_any_hidden_content(shadow_root):
					return True

			return False

		def process_node(current_node: EnhancedDOMTreeNode) -> None:
			"""Process node and descendants, collecting hidden elements for iframes."""
			if (
				current_node.node_type == NodeType.ELEMENT_NODE
				and current_node.tag_name
				and current_node.tag_name.upper() in ('IFRAME', 'FRAME')
				and current_node.content_document
			):
				# Get viewport height from iframe's client rect
				viewport_height = 0.0
				if current_node.snapshot_node and current_node.snapshot_node.clientRects:
					viewport_height = current_node.snapshot_node.clientRects.height

				hidden = collect_hidden_elements(current_node.content_document, viewport_height)
				# Sort by pages and limit to avoid bloating context
				hidden.sort(key=lambda x: x['pages'])
				current_node.hidden_elements_info = hidden[:10]  # Limit to 10

				# Check for hidden non-interactive content when no interactive elements found
				if not hidden and has_any_hidden_content(current_node.content_document):
					current_node.has_hidden_content = True

			for child in current_node.children_nodes or []:
				process_node(child)

			if current_node.content_document:
				process_node(current_node.content_document)

			for shadow_root in current_node.shadow_roots or []:
				process_node(shadow_root)

		process_node(node)

	@classmethod
	def is_element_visible_according_to_all_parents(
		cls, node: EnhancedDOMTreeNode, html_frames: list[EnhancedDOMTreeNode], viewport_threshold: int | None = 1000
	) -> bool:
		"""Check if the element is visible according to all its parent HTML frames.

		Args:
			node: The DOM node to check visibility for
			html_frames: List of parent HTML frame nodes
			viewport_threshold: Pixel threshold beyond viewport to consider visible.
				Default 1000px. Set to None to disable threshold checking entirely.
		"""

		if not node.snapshot_node:
			return False

		computed_styles = node.snapshot_node.computed_styles or {}

		display = computed_styles.get('display', '').lower()
		visibility = computed_styles.get('visibility', '').lower()
		opacity = computed_styles.get('opacity', '1')

		if display == 'none' or visibility == 'hidden':
			return False

		try:
			if float(opacity) <= 0:
				return False
		except (ValueError, TypeError):
			pass

		if not node.snapshot_node.bounds:
			return False  # If there are no bounds, the element is not visible

		# work on a copy: snapshot bounds are shared, in-place mutation corrupts other consumers
		current_bounds = DOMRect(
			x=node.snapshot_node.bounds.x,
			y=node.snapshot_node.bounds.y,
			width=node.snapshot_node.bounds.width,
			height=node.snapshot_node.bounds.height,
		)

		# If threshold is None, skip all viewport-based filtering (only check CSS visibility)
		if viewport_threshold is None:
			return True

		"""
		Reverse iterate through the html frames (that can be either iframe or document -> if it's a document frame compare if the current bounds interest with it (taking scroll into account) otherwise move the current bounds by the iframe offset)
		"""
		for frame in reversed(html_frames):
			# skip self: a frame node appears in its own frame chain and must not offset itself
			if frame is node:
				continue
			if (
				frame.node_type == NodeType.ELEMENT_NODE
				and (frame.node_name.upper() == 'IFRAME' or frame.node_name.upper() == 'FRAME')
				and frame.snapshot_node
				and frame.snapshot_node.bounds
			):
				iframe_bounds = frame.snapshot_node.bounds

				# negate the values added in `_construct_enhanced_node`
				current_bounds.x += iframe_bounds.x
				current_bounds.y += iframe_bounds.y

			if (
				frame.node_type == NodeType.ELEMENT_NODE
				and frame.node_name == 'HTML'
				and frame.snapshot_node
				and frame.snapshot_node.scrollRects
				and frame.snapshot_node.clientRects
			):
				# For iframe content, we need to check visibility within the iframe's viewport
				# The scrollRects represent the current scroll position
				# The clientRects represent the viewport size
				# Elements are visible if they fall within the viewport after accounting for scroll

				# The viewport of the frame (what's actually visible)
				viewport_left = 0  # Viewport always starts at 0 in frame coordinates
				viewport_top = 0
				viewport_right = frame.snapshot_node.clientRects.width
				viewport_bottom = frame.snapshot_node.clientRects.height

				# Adjust element bounds by the scroll offset to get position relative to viewport
				# When scrolled down, scrollRects.y is positive, so we subtract it from element's y
				adjusted_x = current_bounds.x - frame.snapshot_node.scrollRects.x
				adjusted_y = current_bounds.y - frame.snapshot_node.scrollRects.y

				frame_intersects = (
					adjusted_x < viewport_right
					and adjusted_x + current_bounds.width > viewport_left
					and adjusted_y < viewport_bottom + viewport_threshold
					and adjusted_y + current_bounds.height > viewport_top - viewport_threshold
				)

				if not frame_intersects:
					return False

				# Keep the original coordinate adjustment to maintain consistency
				# This adjustment is needed for proper coordinate transformation
				current_bounds.x -= frame.snapshot_node.scrollRects.x
				current_bounds.y -= frame.snapshot_node.scrollRects.y

		# If we reach here, element is visible in main viewport and all containing iframes
		return True

	async def get_dom_tree(
		self,
		target_id: TargetID,
		all_frames: dict | None = None,
		initial_html_frames: list[EnhancedDOMTreeNode] | None = None,
		initial_total_frame_offset: DOMRect | None = None,
		iframe_depth: int = 0,
		visited_cross_origin_targets: set[TargetID] | None = None,
	) -> tuple[EnhancedDOMTreeNode, dict[str, float]]:
		"""Get the DOM tree for a specific target.

		Args:
			target_id: Target ID of the page to get the DOM tree for.
			all_frames: Pre-fetched frame hierarchy to avoid redundant CDP calls (optional, lazy fetch if None)
			initial_html_frames: List of HTML frame nodes encountered so far
			initial_total_frame_offset: Accumulated coordinate offset
			iframe_depth: Current depth of iframe nesting to prevent infinite recursion
			visited_cross_origin_targets: Target IDs already included in this DOM capture

		Returns:
			Tuple of (enhanced_dom_tree_node, timing_info)
		"""
		if visited_cross_origin_targets is None:
			visited_cross_origin_targets = {target_id}

		timing_info: dict[str, float] = {}
		timing_start_total = time.time()

		# Get all trees from CDP (snapshot, DOM, AX, viewport ratio)
		start_get_trees = time.time()
		trees = await self.collector.collect(target_id)
		get_trees_ms = (time.time() - start_get_trees) * 1000
		timing_info.update(trees.cdp_timing)
		timing_info['get_all_trees_total_ms'] = get_trees_ms

		dom_tree = trees.dom_tree
		ax_tree = trees.ax_tree
		snapshot = trees.snapshot
		device_pixel_ratio = trees.device_pixel_ratio
		js_click_listener_backend_ids = trees.js_click_listener_backend_ids or set()

		# Build AX tree lookup
		start_ax = time.time()
		ax_tree_lookup: dict[int, AXNode] = {
			ax_node['backendDOMNodeId']: ax_node for ax_node in ax_tree['nodes'] if 'backendDOMNodeId' in ax_node
		}
		timing_info['build_ax_lookup_ms'] = (time.time() - start_ax) * 1000

		enhanced_dom_tree_node_lookup: dict[int, EnhancedDOMTreeNode] = {}
		""" NodeId (NOT backend node id) -> enhanced dom tree node"""  # way to get the parent/content node

		# Parse snapshot data with everything calculated upfront
		start_snapshot = time.time()
		snapshot_lookup = build_snapshot_lookup(snapshot, device_pixel_ratio)
		timing_info['build_snapshot_lookup_ms'] = (time.time() - start_snapshot) * 1000

		builder = EnhancedDOMTreeBuilder(
			service=self,
			target_id=target_id,
			iframe_depth=iframe_depth,
			visited_cross_origin_targets=visited_cross_origin_targets,
			ax_tree_lookup=ax_tree_lookup,
			snapshot_lookup=snapshot_lookup,
			js_click_listener_backend_ids=js_click_listener_backend_ids,
			node_lookup=enhanced_dom_tree_node_lookup,
		)

		# Build enhanced DOM tree recursively. Cross-origin targets call back into
		# this orchestration method so each target receives its own collection pass.
		start_construct = time.time()
		enhanced_dom_tree_node = await builder.build(
			dom_tree['root'], initial_html_frames, initial_total_frame_offset, all_frames
		)
		timing_info['construct_enhanced_tree_ms'] = (time.time() - start_construct) * 1000

		# Count hidden elements per iframe for LLM hints
		self._count_hidden_elements_in_iframes(enhanced_dom_tree_node)

		# Calculate total time for get_dom_tree
		total_get_dom_tree_ms = (time.time() - timing_start_total) * 1000
		timing_info['get_dom_tree_total_ms'] = total_get_dom_tree_ms

		# Calculate overhead in get_dom_tree (time not accounted for by sub-operations)
		tracked_sub_operations_ms = (
			timing_info.get('get_all_trees_total_ms', 0)
			+ timing_info.get('build_ax_lookup_ms', 0)
			+ timing_info.get('build_snapshot_lookup_ms', 0)
			+ timing_info.get('construct_enhanced_tree_ms', 0)
		)
		get_dom_tree_overhead_ms = total_get_dom_tree_ms - tracked_sub_operations_ms
		if get_dom_tree_overhead_ms > 0.1:
			timing_info['get_dom_tree_overhead_ms'] = get_dom_tree_overhead_ms

		return enhanced_dom_tree_node, timing_info

	async def get_serialized_dom_tree(
		self, previous_cached_state: SerializedDOMState | None = None
	) -> tuple[SerializedDOMState, EnhancedDOMTreeNode, dict[str, float]]:
		"""Get the serialized DOM tree representation for LLM consumption.

		Returns:
			Tuple of (serialized_dom_state, enhanced_dom_tree_root, timing_info)
		"""
		timing_info: dict[str, float] = {}
		start_total = time.time()

		# Use current target (None means use current)
		assert self.browser_session.agent_focus_target_id is not None

		session_id = self.browser_session.id

		# Build DOM tree (includes CDP calls for snapshot, DOM, AX tree)
		# Note: all_frames is fetched lazily inside get_dom_tree only if cross-origin iframes need it
		enhanced_dom_tree, dom_tree_timing = await self.get_dom_tree(
			target_id=self.browser_session.agent_focus_target_id,
			all_frames=None,  # Lazy - will fetch if needed
		)

		# Add sub-timings from DOM tree construction
		timing_info.update(dom_tree_timing)

		# Serialize DOM tree for LLM
		start_serialize = time.time()

		serialized_dom_state, serializer_timing = DOMTreeSerializer(
			enhanced_dom_tree, previous_cached_state, paint_order_filtering=self.paint_order_filtering, session_id=session_id
		).serialize_accessible_elements()
		total_serialization_ms = (time.time() - start_serialize) * 1000

		# Add serializer sub-timings (convert to ms)
		for key, value in serializer_timing.items():
			timing_info[f'{key}_ms'] = value * 1000

		# Calculate untracked time in serialization
		tracked_serialization_ms = sum(value * 1000 for value in serializer_timing.values())
		serialization_overhead_ms = total_serialization_ms - tracked_serialization_ms
		if serialization_overhead_ms > 0.1:  # Only log if significant
			timing_info['serialization_overhead_ms'] = serialization_overhead_ms

		# Calculate total time for get_serialized_dom_tree
		total_get_serialized_dom_tree_ms = (time.time() - start_total) * 1000
		timing_info['get_serialized_dom_tree_total_ms'] = total_get_serialized_dom_tree_ms

		# Calculate overhead in get_serialized_dom_tree (time not accounted for)
		tracked_major_operations_ms = timing_info.get('get_dom_tree_total_ms', 0) + total_serialization_ms
		get_serialized_overhead_ms = total_get_serialized_dom_tree_ms - tracked_major_operations_ms
		if get_serialized_overhead_ms > 0.1:
			timing_info['get_serialized_dom_tree_overhead_ms'] = get_serialized_overhead_ms

		return serialized_dom_state, enhanced_dom_tree, timing_info

	@staticmethod
	def detect_pagination_buttons(selector_map: dict[int, EnhancedDOMTreeNode]) -> list[dict[str, str | int | bool]]:
		"""Detect pagination buttons from the selector map.

		Args:
			selector_map: Map of element indices to EnhancedDOMTreeNode

		Returns:
			List of pagination button information dicts with:
			- button_type: 'next', 'prev', 'first', 'last', 'page_number'
			- backend_node_id: Backend node ID for clicking
			- selector_index: Model-visible selector index
			- text: Button text/label
			- selector: XPath selector
			- is_disabled: Whether the button appears disabled
		"""
		pagination_buttons: list[dict[str, str | int | bool]] = []

		# Common pagination patterns to look for
		# `«` and `»` are ambiguous across sites, so treat them only as prev/next
		# fallback symbols and let word-based first/last signals win
		next_patterns = ['next', '>', '»', '→', 'siguiente', 'suivant', 'weiter', 'volgende']
		prev_patterns = ['prev', 'previous', '<', '«', '←', 'anterior', 'précédent', 'zurück', 'vorige']
		first_patterns = ['first', '⇤', 'primera', 'première', 'erste', 'eerste']
		last_patterns = ['last', '⇥', 'última', 'dernier', 'letzte', 'laatste']

		for index, node in selector_map.items():
			# Skip non-clickable elements
			if not node.snapshot_node or not node.snapshot_node.is_clickable:
				continue

			# Get element text and attributes
			text = node.get_all_children_text().lower().strip()
			aria_label = node.attributes.get('aria-label', '').lower()
			title = node.attributes.get('title', '').lower()
			class_name = node.attributes.get('class', '').lower()
			role = node.attributes.get('role', '').lower()

			# Combine all text sources for pattern matching
			all_text = f'{text} {aria_label} {title} {class_name}'.strip()

			# Check if it's disabled
			is_disabled = (
				node.attributes.get('disabled') == 'true'
				or node.attributes.get('aria-disabled') == 'true'
				or 'disabled' in class_name
			)

			button_type: str | None = None

			# Match specific first/last semantics before generic prev/next fallbacks.
			if any(pattern in all_text for pattern in first_patterns):
				button_type = 'first'
			# Check for last button
			elif any(pattern in all_text for pattern in last_patterns):
				button_type = 'last'
			# Check for next button
			elif any(pattern in all_text for pattern in next_patterns):
				button_type = 'next'
			# Check for previous button
			elif any(pattern in all_text for pattern in prev_patterns):
				button_type = 'prev'
			# Check for numeric page buttons (single or double digit)
			elif text.isdigit() and len(text) <= 2 and role in ['button', 'link', '']:
				button_type = 'page_number'

			if button_type:
				pagination_buttons.append(
					{
						'button_type': button_type,
						'backend_node_id': node.backend_node_id,
						'selector_index': index,
						'text': node.get_all_children_text().strip() or aria_label or title,
						'selector': node.xpath,
						'is_disabled': is_disabled,
					}
				)

		return pagination_buttons
