from __future__ import annotations

from typing import TYPE_CHECKING

from browser_use.agent.history import AgentHistory
from browser_use.dom.history import DOMInteractedElement

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


class ReplayRetryPolicy:
	"""Classify redundant retries and menu recovery steps."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

	def is_redundant_retry_step(
		self,
		current_item: AgentHistory,
		previous_item: AgentHistory | None,
		previous_step_succeeded: bool,
	) -> bool:
		"""
		Detect if current step is a redundant retry of the previous step.

		This handles cases where the original run needed to click the same element multiple
		times due to slow page response, but during replay the first click already succeeded.
		When the page has already navigated, subsequent retry clicks on the same element
		would fail because that element no longer exists.

		Returns True if:
		- Previous step succeeded
		- Both steps target the same element (by element_hash, stable_hash, or xpath)
		- Both steps perform the same action type (e.g., both are clicks)
		"""
		if not previous_item or not previous_step_succeeded:
			return False

		# Get interacted elements from both steps (first action in each)
		curr_elements = current_item.state.interacted_element
		prev_elements = previous_item.state.interacted_element

		if not curr_elements or not prev_elements:
			return False

		curr_elem = curr_elements[0] if curr_elements else None
		prev_elem = prev_elements[0] if prev_elements else None

		if not curr_elem or not prev_elem:
			return False

		# Check if same element by various matching strategies
		same_by_hash = curr_elem.element_hash == prev_elem.element_hash
		same_by_stable_hash = curr_elem.stable_hash == prev_elem.stable_hash
		same_by_xpath = curr_elem.x_path == prev_elem.x_path

		if not (same_by_hash or same_by_stable_hash or same_by_xpath):
			return False

		# Check if same action type
		curr_actions = current_item.model_output.action if current_item.model_output else []
		prev_actions = previous_item.model_output.action if previous_item.model_output else []

		if not curr_actions or not prev_actions:
			return False

		# Get the action type (first key in the action dict)
		curr_action_data = curr_actions[0].model_dump(exclude_unset=True)
		prev_action_data = prev_actions[0].model_dump(exclude_unset=True)

		curr_action_type = next(iter(curr_action_data.keys()), None)
		prev_action_type = next(iter(prev_action_data.keys()), None)

		if curr_action_type != prev_action_type:
			return False

		self.agent.logger.debug(
			f'🔄 Detected redundant retry: both steps target same element '
			f'<{curr_elem.node_name}> with action "{curr_action_type}"'
		)

		return True

	def is_menu_opener_step(self, history_item: AgentHistory | None) -> bool:
		"""
		Detect if a step opens a dropdown/menu.

		Checks for common patterns indicating a menu opener:
		- Element has aria-haspopup attribute
		- Element has data-gw-click="toggleSubMenu" (Guidewire pattern)
		- Element has expand-button in class name
		- Element role is "menuitem" with aria-expanded

		Returns True if the step appears to open a dropdown/submenu.
		"""
		if not history_item or not history_item.state or not history_item.state.interacted_element:
			return False

		elem = history_item.state.interacted_element[0] if history_item.state.interacted_element else None
		if not elem:
			return False

		attrs = elem.attributes or {}

		# Check for common menu opener indicators
		if attrs.get('aria-haspopup') in ('true', 'menu', 'listbox'):
			return True
		if attrs.get('data-gw-click') == 'toggleSubMenu':
			return True
		if 'expand-button' in attrs.get('class', ''):
			return True
		if attrs.get('role') == 'menuitem' and attrs.get('aria-expanded') in ('false', 'true'):
			return True
		if attrs.get('role') == 'button' and attrs.get('aria-expanded') in ('false', 'true'):
			return True

		return False

	def is_menu_item_element(self, elem: DOMInteractedElement | None) -> bool:
		"""
		Detect if an element is a menu item that appears inside a dropdown/menu.

		Checks for:
		- role="menuitem", "option", "menuitemcheckbox", "menuitemradio"
		- Element is inside a menu structure (has menu-related parent indicators)
		- ax_name is set (menu items typically have accessible names)

		Returns True if the element appears to be a menu item.
		"""
		if not elem:
			return False

		attrs = elem.attributes or {}

		# Check for menu item roles
		role = attrs.get('role', '')
		if role in ('menuitem', 'option', 'menuitemcheckbox', 'menuitemradio', 'treeitem'):
			return True

		# Elements in Guidewire menus have these patterns
		if 'gw-action--inner' in attrs.get('class', ''):
			return True
		if 'menuitem' in attrs.get('class', '').lower():
			return True

		# If element has an ax_name and looks like it could be in a menu
		# This is a softer check - only used if the previous step was a menu opener
		if elem.ax_name and elem.ax_name not in ('', None):
			# Common menu container classes
			elem_class = attrs.get('class', '').lower()
			if any(x in elem_class for x in ['dropdown', 'popup', 'menu', 'submenu', 'action']):
				return True

		return False
