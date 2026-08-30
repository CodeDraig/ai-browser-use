from __future__ import annotations

from typing import TYPE_CHECKING

from browser_use.dom.tree import EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class DOMSelectorCache:
	"""Own selector-index mappings for one browser session."""

	def __init__(self, browser_session: BrowserSession) -> None:
		self.browser_session = browser_session
		self.selector_map: dict[int, EnhancedDOMTreeNode] = {}
		self.selector_indices: dict[tuple[str, int], int] = {}

	def clear(self) -> None:
		self.selector_map.clear()
		self.selector_indices.clear()

	def get_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		return self.selector_map.get(index)

	def get_index(self, node: EnhancedDOMTreeNode) -> int:
		return self.selector_indices.get((str(node.session_id), node.backend_node_id), node.backend_node_id)

	def get_by_backend_id(self, backend_node_id: int, session_id: str | None) -> EnhancedDOMTreeNode | None:
		for node in self.selector_map.values():
			if node.backend_node_id == backend_node_id and str(node.session_id) == str(session_id):
				return node
		return None

	def update(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		self.selector_map = selector_map
		self.selector_indices = {(str(node.session_id), node.backend_node_id): index for index, node in selector_map.items()}

	def current(self) -> dict[int, EnhancedDOMTreeNode]:
		if self.selector_map:
			return self.selector_map
		dom_watchdog = self.browser_session.watchdogs.dom
		if dom_watchdog and hasattr(dom_watchdog, 'selector_map'):
			return dom_watchdog.selector_map or {}
		return {}

	def find_index_by_id(self, element_id: str) -> int | None:
		for index, element in self.current().items():
			if element.attributes and element.attributes.get('id') == element_id:
				return index
		return None
