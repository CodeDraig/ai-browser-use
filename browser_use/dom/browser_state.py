"""Browser-session DOM cache, lookup, and highlighting behavior."""

from typing import TYPE_CHECKING, Any

from browser_use.browser.session_manager import CDPSession
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.coordinate_resolver import DOMCoordinateResolver
from browser_use.dom.highlight_state import DOMHighlightState
from browser_use.dom.selector_cache import DOMSelectorCache
from browser_use.dom.tree import DOMRect, EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserDomState:
	"""Own DOM-derived state and element operations for one browser session."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session
		self.selectors = DOMSelectorCache(browser_session)
		self.coordinates = DOMCoordinateResolver(browser_session, self.selectors)
		self.highlights = DOMHighlightState(browser_session, self.coordinates)
		self.cached_browser_state_summary: BrowserStateSummary | None = None
		self.original_viewport_size: tuple[int, int] | None = None

	def clear(self) -> None:
		self.selectors.clear()
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
		return self.selectors.get_by_index(index)

	def get_selector_index(self, node: EnhancedDOMTreeNode) -> int:
		"""Return the model-visible selector index for a DOM node."""
		return self.selectors.get_index(node)

	def _get_cached_node_by_backend_id(self, backend_node_id: int, session_id: str | None) -> EnhancedDOMTreeNode | None:
		"""Resolve a backend ID only within the CDP session that produced it."""
		return self.selectors.get_by_backend_id(backend_node_id, session_id)

	def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		"""Update the cached selector map with new DOM state.

		This should be called by the DOM watchdog after rebuilding the DOM.

		Args:
			selector_map: The new selector map from DOM serialization
		"""
		self.selectors.update(selector_map)

	async def get_dom_element_at_coordinates(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
		return await self.coordinates.get_dom_element_at_coordinates(x, y)

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
		from browser_use.dom.tree import EnhancedDOMTreeNode

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
		return self.selectors.current()

	async def get_index_by_id(self, element_id: str) -> int | None:
		"""Find element index by its id attribute.

		Args:
			element_id: The id attribute value to search for

		Returns:
			Index of the element, or None if not found
		"""
		return self.selectors.find_index_by_id(element_id)

	async def remove_highlights(self) -> None:
		await self.highlights.remove_highlights()

	async def get_element_coordinates(self, backend_node_id: int, cdp_session: CDPSession) -> DOMRect | None:
		return await self.coordinates.get_element_coordinates(backend_node_id, cdp_session)

	async def highlight_interaction_element(self, node: 'EnhancedDOMTreeNode') -> None:
		await self.highlights.highlight_interaction_element(node)

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		await self.highlights.highlight_coordinate_click(x, y)

	async def add_highlights(self, selector_map: dict[int, 'EnhancedDOMTreeNode']) -> None:
		await self.highlights.add_highlights(selector_map)
