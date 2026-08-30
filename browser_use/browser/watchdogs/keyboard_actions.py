"""Raw keyboard input behavior for default browser actions."""

from browser_use.actor.keyboard import KeyboardInteractor
from browser_use.browser.events import SendKeysEvent


class KeyboardActions:
	"""Dispatch page text, key chords, special keys, and character sequences."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session
		self.keyboard_interactor = KeyboardInteractor(browser_session)

	@property
	def logger(self):
		return self.browser_session.logger

	async def type_to_page(self, text: str):
		return await self.keyboard_interactor.type_to_page(text)

	def character_key_info(self, char: str) -> tuple[int, int, str]:
		return self.keyboard_interactor.character_key_info(char)

	def key_code_for_character(self, char: str) -> str:
		return self.keyboard_interactor.key_code_for_character(char)

	async def handle_send_keys(self, event: SendKeysEvent) -> None:
		await self.keyboard_interactor.send_keys(event.keys)
