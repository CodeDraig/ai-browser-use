"""Native and ARIA dropdown behavior for default browser actions."""

from browser_use.actor.dropdown import DropdownInteractor
from browser_use.browser.events import GetDropdownOptionsEvent, SelectDropdownOptionEvent


class DropdownActions:
	"""Inspect and select options in native, ARIA, and custom dropdowns."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session
		self.dropdown_interactor = DropdownInteractor(browser_session)

	@property
	def logger(self):
		return self.browser_session.logger

	async def handle_get_dropdown_options(self, event: GetDropdownOptionsEvent) -> dict[str, str]:
		return await self.dropdown_interactor.get_options(event.node)

	async def _handle_aria_combobox_options(
		self,
		cdp_session,
		object_id: str,
		combobox_info: dict,
		index_for_logging: int,
		backend_node_id: int,
	) -> dict[str, str]:
		return await self.dropdown_interactor._handle_aria_combobox_options(
			cdp_session, object_id, combobox_info, index_for_logging, backend_node_id
		)

	async def handle_select_dropdown_option(self, event: SelectDropdownOptionEvent) -> dict[str, str]:
		return await self.dropdown_interactor.select_option(event.node, event.text)
