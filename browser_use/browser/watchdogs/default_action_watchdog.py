"""Default browser action event endpoint."""

from typing import Any

from pydantic import PrivateAttr

from browser_use.browser.events import (
	ClickCoordinateEvent,
	ClickElementEvent,
	GetDropdownOptionsEvent,
	GoBackEvent,
	GoForwardEvent,
	RefreshEvent,
	ScrollEvent,
	ScrollToTextEvent,
	SelectDropdownOptionEvent,
	SendKeysEvent,
	TypeTextEvent,
	UploadFileEvent,
	WaitEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.browser.watchdogs.click_actions import ClickActions
from browser_use.browser.watchdogs.dropdown_actions import DropdownActions
from browser_use.browser.watchdogs.file_upload_actions import FileUploadActions
from browser_use.browser.watchdogs.keyboard_actions import KeyboardActions
from browser_use.browser.watchdogs.navigation_actions import NavigationActions
from browser_use.browser.watchdogs.scroll_actions import ScrollActions
from browser_use.browser.watchdogs.text_input_actions import TextInputActions
from browser_use.dom.service import EnhancedDOMTreeNode

_EVENT_MODEL_TYPES = {'EnhancedDOMTreeNode': EnhancedDOMTreeNode}
ClickCoordinateEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
ClickElementEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
GetDropdownOptionsEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
SelectDropdownOptionEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
TypeTextEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
ScrollEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)
UploadFileEvent.model_rebuild(_types_namespace=_EVENT_MODEL_TYPES)


class DefaultActionWatchdog(BaseWatchdog):
	"""Route default browser events to cohesive action implementations."""

	_click_actions: ClickActions = PrivateAttr()
	_keyboard_actions: KeyboardActions = PrivateAttr()
	_text_input_actions: TextInputActions = PrivateAttr()
	_scroll_actions: ScrollActions = PrivateAttr()
	_navigation_actions: NavigationActions = PrivateAttr()
	_file_upload_actions: FileUploadActions = PrivateAttr()
	_dropdown_actions: DropdownActions = PrivateAttr()

	def model_post_init(self, __context: Any) -> None:
		"""Bind action implementations to this watchdog's browser session."""
		super().model_post_init(__context)
		self._click_actions = ClickActions(self.browser_session)
		self._keyboard_actions = KeyboardActions(self.browser_session)
		self._text_input_actions = TextInputActions(
			self.browser_session,
			click_actions=self._click_actions,
			keyboard_actions=self._keyboard_actions,
		)
		self._scroll_actions = ScrollActions(self.browser_session)
		self._navigation_actions = NavigationActions(self.browser_session)
		self._file_upload_actions = FileUploadActions(self.browser_session)
		self._dropdown_actions = DropdownActions(self.browser_session)

	async def on_ClickElementEvent(self, event: ClickElementEvent) -> dict | None:
		return await self._click_actions.handle_click_element(event)

	async def on_ClickCoordinateEvent(self, event: ClickCoordinateEvent) -> dict | None:
		return await self._click_actions.handle_click_coordinate(event)

	async def on_TypeTextEvent(self, event: TypeTextEvent) -> dict | None:
		return await self._text_input_actions.handle_type_text(event)

	async def on_ScrollEvent(self, event: ScrollEvent) -> None:
		return await self._scroll_actions.handle_scroll(event)

	async def on_GoBackEvent(self, event: GoBackEvent) -> None:
		return await self._navigation_actions.handle_go_back(event)

	async def on_GoForwardEvent(self, event: GoForwardEvent) -> None:
		return await self._navigation_actions.handle_go_forward(event)

	async def on_RefreshEvent(self, event: RefreshEvent) -> None:
		return await self._navigation_actions.handle_refresh(event)

	async def on_WaitEvent(self, event: WaitEvent) -> None:
		return await self._navigation_actions.handle_wait(event)

	async def on_SendKeysEvent(self, event: SendKeysEvent) -> None:
		return await self._keyboard_actions.handle_send_keys(event)

	async def on_UploadFileEvent(self, event: UploadFileEvent) -> None:
		return await self._file_upload_actions.handle_upload_file(event)

	async def on_ScrollToTextEvent(self, event: ScrollToTextEvent) -> None:
		return await self._scroll_actions.handle_scroll_to_text(event)

	async def on_GetDropdownOptionsEvent(self, event: GetDropdownOptionsEvent) -> dict[str, str]:
		return await self._dropdown_actions.handle_get_dropdown_options(event)

	async def on_SelectDropdownOptionEvent(self, event: SelectDropdownOptionEvent) -> dict[str, str]:
		return await self._dropdown_actions.handle_select_dropdown_option(event)
